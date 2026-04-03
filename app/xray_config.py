import json
import logging
import shutil
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Callable

import portalocker

from .settings import Settings

USER_INBOUND_TAG = "inbound-from-users"
XRAY_API_READY_PATTERN = "user>>>user:"
THROTTLE_OUTBOUND_TAG = "to-exit-throttled"
BLOCKED_OUTBOUND_TAG = "blocked"
ENFORCEMENT_RULE_PREFIX = "enforcement:"

LOG = logging.getLogger(__name__)


class XrayConfigError(RuntimeError):
    pass


def _run_checked(command: list[str], timeout: int = 10) -> subprocess.CompletedProcess:
    return subprocess.run(command, check=True, capture_output=True, text=True, timeout=timeout)


def _load_config(config_path: Path) -> dict:
    with config_path.open("r", encoding="utf-8") as f:
        return json.load(f)


def _get_clients(config: dict) -> list[dict]:
    inbounds = config.get("inbounds", [])
    for inbound in inbounds:
        if inbound.get("tag") == USER_INBOUND_TAG:
            settings = inbound.setdefault("settings", {})
            clients = settings.setdefault("clients", [])
            if not isinstance(clients, list):
                raise XrayConfigError("Xray clients list is not a list")
            return clients

    raise XrayConfigError(f"Inbound with tag '{USER_INBOUND_TAG}' not found")


def _backup_config(config_path: Path) -> Path:
    backup_path = Path(f"{config_path}.bak.{int(time.time())}")
    shutil.copy2(config_path, backup_path)
    return backup_path


def _atomic_write(config_path: Path, config: dict) -> None:
    config_path.parent.mkdir(parents=True, exist_ok=True)

    with tempfile.NamedTemporaryFile(
        mode="w",
        encoding="utf-8",
        dir=str(config_path.parent),
        prefix="config.json.",
        suffix=".tmp",
        delete=False,
    ) as tmp_file:
        json.dump(config, tmp_file, ensure_ascii=True, indent=2)
        tmp_file.write("\n")
        tmp_name = tmp_file.name

    Path(tmp_name).replace(config_path)


def _validate_xray_config(settings: Settings) -> None:
    _run_checked([settings.xray_bin, "run", "-test", "-config", settings.xray_config], timeout=15)


def _restart_xray(settings: Settings) -> None:
    _run_checked(["systemctl", "restart", settings.xray_service], timeout=20)


def _probe_xray_api_ready(settings: Settings, probe_timeout: int = 4) -> bool:
    result = subprocess.run(
        [
            settings.xray_bin,
            "api",
            "statsquery",
            "--server",
            settings.xray_api_addr,
            "-pattern",
            XRAY_API_READY_PATTERN,
        ],
        capture_output=True,
        text=True,
        timeout=probe_timeout,
    )
    if result.returncode != 0:
        return False

    raw = (result.stdout or "").strip()
    if not raw:
        return False

    try:
        json.loads(raw)
        return True
    except json.JSONDecodeError:
        start = raw.find("{")
        end = raw.rfind("}")
        if start == -1 or end == -1 or end <= start:
            return False
        try:
            json.loads(raw[start : end + 1])
            return True
        except json.JSONDecodeError:
            return False


def _wait_for_xray_api_ready(
    settings: Settings,
    *,
    ready_timeout: float = 20.0,
    interval_seconds: float = 0.5,
) -> None:
    deadline = time.monotonic() + ready_timeout
    last_error = "xray api did not become ready in time"
    time.sleep(0.2)

    while time.monotonic() < deadline:
        try:
            if _probe_xray_api_ready(settings):
                return
            last_error = "xray api probe returned no valid stats response"
        except Exception as exc:
            last_error = str(exc)
        time.sleep(interval_seconds)

    raise XrayConfigError(f"Xray API is not ready after restart: {last_error}")


def _apply_mutation(settings: Settings, mutator: Callable[[dict], bool]) -> bool:
    config_path = Path(settings.xray_config)
    lock_path = Path(f"{settings.xray_config}.lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)

    with lock_path.open("a+", encoding="utf-8") as lock_file:
        portalocker.lock(lock_file, portalocker.LOCK_EX)

        config = _load_config(config_path)
        changed = mutator(config)
        if not changed:
            return False

        backup_path = _backup_config(config_path)
        _atomic_write(config_path, config)

        try:
            _validate_xray_config(settings)
            _restart_xray(settings)
            _wait_for_xray_api_ready(settings)
            return True
        except Exception as exc:
            shutil.copy2(backup_path, config_path)
            try:
                _validate_xray_config(settings)
            except Exception:
                pass
            try:
                _restart_xray(settings)
            except Exception:
                pass
            try:
                _wait_for_xray_api_ready(settings)
            except Exception:
                pass
            raise XrayConfigError(f"Failed to apply Xray config: {exc}") from exc


def upsert_user_client(settings: Settings, user_id: str, user_uuid: str) -> bool:
    email = f"user:{user_id}"
    flow = settings.user_flow.strip() if settings.user_flow else ""

    def mutate(config: dict) -> bool:
        clients = _get_clients(config)

        for client in clients:
            if client.get("email") == email:
                changed = False
                if client.get("id") != user_uuid:
                    client["id"] = user_uuid
                    changed = True
                if client.get("email") != email:
                    client["email"] = email
                    changed = True
                if flow:
                    if client.get("flow") != flow:
                        client["flow"] = flow
                        changed = True
                else:
                    if "flow" in client:
                        client.pop("flow", None)
                        changed = True
                return changed

        new_client = {"id": user_uuid, "email": email}
        if flow:
            new_client["flow"] = flow
        clients.append(new_client)
        return True

    return _apply_mutation(settings, mutate)


def remove_user_client(settings: Settings, user_id: str) -> bool:
    email = f"user:{user_id}"

    def mutate(config: dict) -> bool:
        clients = _get_clients(config)
        initial_len = len(clients)
        clients[:] = [client for client in clients if client.get("email") != email]
        return len(clients) != initial_len

    return _apply_mutation(settings, mutate)


def _ensure_throttle_outbound(config: dict, settings: Settings) -> None:
    outbounds = config.setdefault("outbounds", [])
    for ob in outbounds:
        if ob.get("tag") == THROTTLE_OUTBOUND_TAG:
            ss = ob.setdefault("streamSettings", {})
            ss.setdefault("sockopt", {})["mark"] = settings.limited_tc_mark
            return

    exit_ob = None
    for ob in outbounds:
        if ob.get("tag") == "to-exit":
            exit_ob = ob
            break

    if exit_ob is None:
        raise XrayConfigError("Outbound 'to-exit' not found; cannot create throttle outbound")

    import copy
    throttle_ob = copy.deepcopy(exit_ob)
    throttle_ob["tag"] = THROTTLE_OUTBOUND_TAG
    ss = throttle_ob.setdefault("streamSettings", {})
    ss.setdefault("sockopt", {})["mark"] = settings.limited_tc_mark
    outbounds.append(throttle_ob)


def _ensure_blocked_outbound(config: dict) -> None:
    outbounds = config.setdefault("outbounds", [])
    for ob in outbounds:
        if ob.get("tag") == BLOCKED_OUTBOUND_TAG:
            return
    outbounds.append({"protocol": "blackhole", "tag": BLOCKED_OUTBOUND_TAG, "settings": {}})


def _is_enforcement_rule(rule: dict) -> bool:
    return isinstance(rule.get("attrs", {}).get("_enforcement"), bool)


def apply_enforcement_routing(
    settings: Settings,
    throttled_user_ids: list[str],
    blocked_user_ids: list[str],
) -> bool:
    throttled_emails = [f"user:{uid}" for uid in throttled_user_ids]
    blocked_emails = [f"user:{uid}" for uid in blocked_user_ids]

    def mutate(config: dict) -> bool:
        _ensure_throttle_outbound(config, settings)
        _ensure_blocked_outbound(config)

        routing = config.setdefault("routing", {})
        rules = routing.setdefault("rules", [])

        old_enforcement = [r for r in rules if _is_enforcement_rule(r)]
        rules[:] = [r for r in rules if not _is_enforcement_rule(r)]

        new_enforcement: list[dict] = []

        if blocked_emails:
            new_enforcement.append({
                "type": "field",
                "user": blocked_emails,
                "inboundTag": [USER_INBOUND_TAG],
                "outboundTag": BLOCKED_OUTBOUND_TAG,
                "attrs": {"_enforcement": True},
            })

        if throttled_emails:
            new_enforcement.append({
                "type": "field",
                "user": throttled_emails,
                "inboundTag": [USER_INBOUND_TAG],
                "outboundTag": THROTTLE_OUTBOUND_TAG,
                "attrs": {"_enforcement": True},
            })

        api_rule_idx = None
        for i, r in enumerate(rules):
            if r.get("outboundTag") == "api":
                api_rule_idx = i
                break

        insert_at = (api_rule_idx + 1) if api_rule_idx is not None else 0
        for j, er in enumerate(new_enforcement):
            rules.insert(insert_at + j, er)

        if len(new_enforcement) != len(old_enforcement):
            return True
        for new_r, old_r in zip(new_enforcement, old_enforcement):
            if new_r.get("user") != old_r.get("user") or new_r.get("outboundTag") != old_r.get("outboundTag"):
                return True
        return False

    return _apply_mutation(settings, mutate)
