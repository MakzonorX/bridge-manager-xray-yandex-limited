from __future__ import annotations

import json
import logging
import re
import subprocess
import threading
from datetime import datetime, timezone

from .models import UserTraffic
from .settings import Settings
from .storage import get_session

LOG = logging.getLogger(__name__)
USER_STAT_RE = re.compile(r"^user>>>user:(.+)>>>traffic>>>(uplink|downlink)$")


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


def _merge_user_snapshot(user_id: str, runtime_uplink: int, runtime_downlink: int) -> tuple[int, int]:
    now = datetime.now(timezone.utc)
    session = get_session()
    try:
        traffic = session.get(UserTraffic, user_id)
        if traffic is None:
            traffic = UserTraffic(
                user_id=user_id,
                total_uplink=max(runtime_uplink, 0),
                total_downlink=max(runtime_downlink, 0),
                last_runtime_uplink=max(runtime_uplink, 0),
                last_runtime_downlink=max(runtime_downlink, 0),
                updated_at=now,
            )
            session.add(traffic)
            session.commit()
            return traffic.total_uplink, traffic.total_downlink

        traffic.total_uplink += _delta(traffic.last_runtime_uplink, runtime_uplink)
        traffic.total_downlink += _delta(traffic.last_runtime_downlink, runtime_downlink)
        traffic.last_runtime_uplink = max(runtime_uplink, 0)
        traffic.last_runtime_downlink = max(runtime_downlink, 0)
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


def fetch_user_runtime_stats(settings: Settings, user_id: str) -> tuple[int, int] | None:
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

    # If both calls failed, caller may use persisted totals only.
    if uplink_payload is None and downlink_payload is None:
        return None

    uplink = 0
    downlink = 0

    if uplink_payload is not None:
        stat = uplink_payload.get("stat", {})
        if isinstance(stat, dict):
            uplink = int(stat.get("value", 0))

    if downlink_payload is not None:
        stat = downlink_payload.get("stat", {})
        if isinstance(stat, dict):
            downlink = int(stat.get("value", 0))

    return uplink, downlink


def fetch_all_user_runtime_stats(settings: Settings) -> dict[str, dict[str, int]]:
    payload = _run_xray_api_json(
        settings,
        ["statsquery", "--server", settings.xray_api_addr, "-pattern", "user>>>user:"],
    )
    if payload is None:
        return {}

    stats = payload.get("stat", [])
    if not isinstance(stats, list):
        return {}

    users: dict[str, dict[str, int]] = {}

    for item in stats:
        if not isinstance(item, dict):
            continue
        name = item.get("name")
        value = item.get("value")
        if not isinstance(name, str) or not isinstance(value, int):
            continue

        match = USER_STAT_RE.match(name)
        if not match:
            continue

        user_id = match.group(1)
        direction = match.group(2)
        if user_id not in users:
            users[user_id] = {"uplink": 0, "downlink": 0}

        users[user_id][direction] = value

    return users


def persist_all_user_runtime_totals(settings: Settings) -> None:
    snapshots = fetch_all_user_runtime_stats(settings)
    for user_id, values in snapshots.items():
        _merge_user_snapshot(user_id, values.get("uplink", 0), values.get("downlink", 0))


def get_user_traffic(settings: Settings, user_id: str) -> dict[str, int]:
    runtime = fetch_user_runtime_stats(settings, user_id)
    if runtime is not None:
        total_up, total_down = _merge_user_snapshot(user_id, runtime[0], runtime[1])
        runtime_up, runtime_down = runtime
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
