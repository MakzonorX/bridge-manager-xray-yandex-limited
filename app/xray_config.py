import json
import shutil
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Callable

import portalocker

from .settings import Settings

USER_INBOUND_TAG = "inbound-from-users"


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
            raise XrayConfigError(f"Failed to apply Xray config: {exc}") from exc


def upsert_user_client(settings: Settings, user_id: str, user_uuid: str) -> bool:
    email = f"user:{user_id}"

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
                return changed

        clients.append({"id": user_uuid, "email": email})
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
