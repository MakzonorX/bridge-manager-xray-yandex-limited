from __future__ import annotations

import json
import logging
import re
import subprocess
import threading
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone

from .models import UserTraffic
from .settings import Settings
from .storage import get_session

LOG = logging.getLogger(__name__)
USER_STAT_RE = re.compile(r"^user>>>user:(.+)>>>traffic>>>(uplink|downlink)$")
_TRAFFIC_STATE_LOCK = threading.RLock()


@dataclass(frozen=True)
class RuntimeSnapshot:
    uplink: int | None = None
    downlink: int | None = None

    def has_values(self) -> bool:
        return self.uplink is not None or self.downlink is not None

    def response_values(self) -> tuple[int, int]:
        return self.uplink or 0, self.downlink or 0


@contextmanager
def traffic_state_guard():
    with _TRAFFIC_STATE_LOCK:
        yield


def _run_xray_api_json(settings: Settings, args: list[str], timeout: int = 8) -> dict | None:
    command = [settings.xray_bin, "api", *args]
    try:
        result = subprocess.run(command, capture_output=True, text=True, timeout=timeout)
    except Exception as exc:
        LOG.warning("xray api call failed: %s", exc)
        return None

    if result.returncode != 0:
        stderr = (result.stderr or "").strip()
        if stderr:
            LOG.debug("xray api call returned non-zero: %s", stderr)
        return None

    raw = (result.stdout or "").strip()
    if not raw:
        return None

    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        # Some builds may print non-JSON text around payload; best-effort extraction.
        start = raw.find("{")
        end = raw.rfind("}")
        if start == -1 or end == -1 or end <= start:
            LOG.warning("xray api output is not JSON")
            return None
        try:
            return json.loads(raw[start : end + 1])
        except json.JSONDecodeError:
            LOG.warning("xray api output JSON parsing failed")
            return None


def _delta(last_runtime: int, current_runtime: int) -> int:
    if current_runtime < 0:
        current_runtime = 0
    if last_runtime < 0:
        last_runtime = 0

    if current_runtime >= last_runtime:
        return current_runtime - last_runtime
    # Counter reset (e.g. xray restart). Count from zero.
    return current_runtime


def _coerce_runtime_value(payload: dict | None) -> int | None:
    if payload is None:
        return None

    stat = payload.get("stat", {})
    if not isinstance(stat, dict) or "value" not in stat:
        return None

    value = stat.get("value")
    try:
        return max(int(value), 0)
    except (TypeError, ValueError):
        return None


def _merge_user_snapshot(
    user_id: str,
    runtime_uplink: int | None,
    runtime_downlink: int | None,
) -> tuple[int, int]:
    if runtime_uplink is None and runtime_downlink is None:
        return _read_totals(user_id)

    now = datetime.now(timezone.utc)
    session = get_session()
    try:
        traffic = session.get(UserTraffic, user_id)
        if traffic is None:
            traffic = UserTraffic(
                user_id=user_id,
                total_uplink=0,
                total_downlink=0,
                last_runtime_uplink=0,
                last_runtime_downlink=0,
                updated_at=now,
            )

        if runtime_uplink is not None:
            traffic.total_uplink += _delta(traffic.last_runtime_uplink, runtime_uplink)
            traffic.last_runtime_uplink = runtime_uplink

        if runtime_downlink is not None:
            traffic.total_downlink += _delta(traffic.last_runtime_downlink, runtime_downlink)
            traffic.last_runtime_downlink = runtime_downlink

        traffic.updated_at = now
        session.add(traffic)
        session.commit()
        return traffic.total_uplink, traffic.total_downlink
    finally:
        session.close()


def _read_totals(user_id: str) -> tuple[int, int]:
    session = get_session()
    try:
        traffic = session.get(UserTraffic, user_id)
        if traffic is None:
            return 0, 0
        return traffic.total_uplink, traffic.total_downlink
    finally:
        session.close()


def fetch_user_runtime_stats(settings: Settings, user_id: str) -> RuntimeSnapshot | None:
    uplink_name = f"user>>>user:{user_id}>>>traffic>>>uplink"
    downlink_name = f"user>>>user:{user_id}>>>traffic>>>downlink"

    uplink_payload = _run_xray_api_json(
        settings,
        ["stats", "--server", settings.xray_api_addr, "-name", uplink_name],
    )
    downlink_payload = _run_xray_api_json(
        settings,
        ["stats", "--server", settings.xray_api_addr, "-name", downlink_name],
    )

    snapshot = RuntimeSnapshot(
        uplink=_coerce_runtime_value(uplink_payload),
        downlink=_coerce_runtime_value(downlink_payload),
    )
    if not snapshot.has_values():
        return None

    return snapshot


def fetch_all_user_runtime_stats(settings: Settings) -> dict[str, RuntimeSnapshot]:
    payload = _run_xray_api_json(
        settings,
        ["statsquery", "--server", settings.xray_api_addr, "-pattern", "user>>>user:"],
    )
    if payload is None:
        return {}

    stats = payload.get("stat", [])
    if not isinstance(stats, list):
        return {}

    users: dict[str, dict[str, int | None]] = {}

    for item in stats:
        if not isinstance(item, dict):
            continue
        name = item.get("name")
        value = item.get("value")
        if not isinstance(name, str):
            continue

        match = USER_STAT_RE.match(name)
        if not match:
            continue

        try:
            coerced_value = max(int(value), 0)
        except (TypeError, ValueError):
            continue

        user_id = match.group(1)
        direction = match.group(2)
        if user_id not in users:
            users[user_id] = {"uplink": None, "downlink": None}

        users[user_id][direction] = coerced_value

    return {
        user_id: RuntimeSnapshot(uplink=values["uplink"], downlink=values["downlink"])
        for user_id, values in users.items()
    }


def persist_all_user_runtime_totals(settings: Settings) -> None:
    with traffic_state_guard():
        snapshots = fetch_all_user_runtime_stats(settings)
        for user_id, snapshot in snapshots.items():
            _merge_user_snapshot(user_id, snapshot.uplink, snapshot.downlink)


def get_user_traffic(settings: Settings, user_id: str) -> dict[str, int]:
    with traffic_state_guard():
        runtime = fetch_user_runtime_stats(settings, user_id)
        if runtime is not None:
            total_up, total_down = _merge_user_snapshot(user_id, runtime.uplink, runtime.downlink)
            runtime_up, runtime_down = runtime.response_values()
        else:
            total_up, total_down = _read_totals(user_id)
            runtime_up, runtime_down = 0, 0

    return {
        "uplink_bytes": total_up,
        "downlink_bytes": total_down,
        "runtime_uplink_bytes": runtime_up,
        "runtime_downlink_bytes": runtime_down,
    }


class TrafficCollector:
    def __init__(self, settings: Settings, interval_seconds: int = 15) -> None:
        self.settings = settings
        self.interval_seconds = max(interval_seconds, 5)
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._thread = threading.Thread(target=self._run, name="traffic-collector", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop_event.set()
        if self._thread is not None:
            self._thread.join(timeout=2)

    def _run(self) -> None:
        while not self._stop_event.wait(self.interval_seconds):
            try:
                persist_all_user_runtime_totals(self.settings)
            except Exception as exc:
                LOG.warning("traffic collector iteration failed: %s", exc)
