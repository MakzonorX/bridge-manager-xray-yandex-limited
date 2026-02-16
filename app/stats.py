import re
import subprocess

from .settings import Settings


def _read_counter(settings: Settings, name: str) -> int:
    command = [
        settings.xray_bin,
        "api",
        "stats",
        "--server",
        settings.xray_api_addr,
        "-name",
        name,
    ]

    try:
        result = subprocess.run(command, capture_output=True, text=True, timeout=5)
    except Exception:
        return 0

    if result.returncode != 0:
        return 0

    text = (result.stdout or "") + "\n" + (result.stderr or "")
    match = re.search(r"value:\s*(\d+)", text)
    if not match:
        return 0

    return int(match.group(1))


def get_user_traffic(settings: Settings, user_id: str) -> dict[str, int]:
    uplink_name = f"user>>>user:{user_id}>>>traffic>>>uplink"
    downlink_name = f"user>>>user:{user_id}>>>traffic>>>downlink"

    return {
        "uplink_bytes": _read_counter(settings, uplink_name),
        "downlink_bytes": _read_counter(settings, downlink_name),
    }
