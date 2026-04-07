from datetime import datetime, timezone
from typing import Literal, Optional
from uuid import uuid4
from urllib.parse import quote
import logging
import os
import socket
import subprocess

from fastapi import Depends, FastAPI, HTTPException, status
from fastapi.responses import JSONResponse, PlainTextResponse
from pydantic import BaseModel, Field, model_validator

from .auth import require_token
from .enforcement import (
    EnforcementLoop,
    check_and_enforce_limits,
    clear_enforcement,
    get_or_create_policy,
    reapply_enforcement_routing,
)
from .models import User, UserLimitPolicy, UserTraffic
from .settings import Settings, get_settings
from .stats import TrafficCollector, get_user_traffic, persist_all_user_runtime_totals, traffic_state_guard
from .storage import get_session, init_db
from .xray_config import XrayConfigError, remove_user_client, upsert_user_client

app = FastAPI(title="Bridge Manager", version="1.1.0")
LOG = logging.getLogger(__name__)
traffic_collector: TrafficCollector | None = None
enforcement_loop: EnforcementLoop | None = None


class CreateUserRequest(BaseModel):
    user_id: str = Field(min_length=1, max_length=128)
    label: str | None = Field(default=None, max_length=255)


class LimitPolicyRequest(BaseModel):
    mode: Literal["unlimited", "limited"]
    traffic_limit_bytes: Optional[int] = None
    post_limit_action: Optional[Literal["throttle", "block"]] = None
    throttle_rate_bytes_per_sec: Optional[int] = None

    @model_validator(mode="after")
    def validate_policy(self):
        if self.mode == "unlimited":
            self.traffic_limit_bytes = None
            self.post_limit_action = None
            self.throttle_rate_bytes_per_sec = None
        elif self.mode == "limited":
            if self.traffic_limit_bytes is None or self.traffic_limit_bytes <= 0:
                raise ValueError("traffic_limit_bytes must be > 0 for limited mode")
            if self.post_limit_action is None:
                raise ValueError("post_limit_action is required for limited mode")
            if self.post_limit_action == "throttle":
                if self.throttle_rate_bytes_per_sec is None:
                    self.throttle_rate_bytes_per_sec = 102400
                if self.throttle_rate_bytes_per_sec <= 0:
                    raise ValueError("throttle_rate_bytes_per_sec must be > 0")
            else:
                self.throttle_rate_bytes_per_sec = None
        return self


def _build_vless_uri(settings: Settings, user_uuid: str, user_id: str, label: str | None) -> str:
    fragment = quote(label or user_id, safe="")
    host = settings.user_host_for_uri or settings.bridge_domain

    if settings.user_transport_mode.lower() == "reality":
        spx = quote(settings.reality_spider_x, safe="")
        return (
            f"vless://{user_uuid}@{host}:{settings.user_port}"
            f"?encryption=none&flow={settings.user_flow}&security=reality"
            f"&sni={settings.reality_server_name}&fp={settings.reality_fingerprint}"
            f"&pbk={settings.reality_public_key}&sid={settings.reality_short_id}"
            f"&type=tcp&spx={spx}"
            f"#{fragment}"
        )

    encoded_path = quote(settings.user_path, safe="")
    return (
        f"vless://{user_uuid}@{host}:{settings.user_port}"
        f"?encryption=none&security=tls&sni={settings.bridge_domain}&type=xhttp&path={encoded_path}"
        f"#{fragment}"
    )


def _serialize_user(settings: Settings, user: User) -> dict:
    payload = {
        "user_id": user.user_id,
        "uuid": user.uuid,
        "label": user.label,
        "created_at": user.created_at.isoformat(),
        "revoked_at": user.revoked_at.isoformat() if user.revoked_at else None,
        "active": user.revoked_at is None,
        "vless_uri": _build_vless_uri(settings, user.uuid, user.user_id, user.label),
        "transport_mode": settings.user_transport_mode,
    }
    if settings.user_transport_mode.lower() == "reality":
        payload["reality"] = {
            "server_name": settings.reality_server_name,
            "public_key": settings.reality_public_key,
            "short_id": settings.reality_short_id,
            "flow": settings.user_flow,
            "fingerprint": settings.reality_fingerprint,
        }

    session = get_session()
    try:
        policy = session.get(UserLimitPolicy, user.user_id)
        if policy is not None:
            payload["limit_policy"] = _serialize_policy(policy, session)
        else:
            payload["limit_policy"] = {
                "mode": "unlimited",
                "traffic_limit_bytes": None,
                "post_limit_action": None,
                "throttle_rate_bytes_per_sec": None,
                "enforcement_state": "none",
                "limit_reached_at": None,
                "total_bytes_observed": 0,
            }
    finally:
        session.close()
    return payload


def _serialize_policy(policy: UserLimitPolicy, session) -> dict:
    traffic = session.get(UserTraffic, policy.user_id)
    total_bytes = 0
    if traffic is not None:
        total_bytes = traffic.total_uplink + traffic.total_downlink

    return {
        "mode": policy.mode,
        "traffic_limit_bytes": policy.traffic_limit_bytes,
        "post_limit_action": policy.post_limit_action,
        "throttle_rate_bytes_per_sec": policy.throttle_rate_bytes_per_sec,
        "enforcement_state": policy.enforcement_state,
        "limit_reached_at": policy.limit_reached_at.isoformat() if policy.limit_reached_at else None,
        "total_bytes_observed": total_bytes,
    }


def _xray_is_active(service_name: str) -> bool:
    result = subprocess.run(["systemctl", "is-active", service_name], capture_output=True, text=True)
    return result.returncode == 0 and result.stdout.strip() == "active"


def _xray_listens_port(port: int) -> bool:
    result = subprocess.run(["ss", "-lnt"], capture_output=True, text=True)
    if result.returncode != 0:
        return False
    return f":{port}" in result.stdout


def _run_command(command: list[str], timeout: float = 5) -> subprocess.CompletedProcess[str] | None:
    try:
        return subprocess.run(command, capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired):
        return None


def _service_state(service_name: str) -> str:
    result = _run_command(["systemctl", "is-active", service_name], timeout=3)
    if result is None:
        return "unknown"
    state = (result.stdout or result.stderr or "").strip()
    return state or "unknown"


def _detect_default_iface() -> str:
    result = _run_command(["ip", "route", "show", "default"], timeout=3)
    if result is None or result.returncode != 0:
        return ""

    for line in result.stdout.splitlines():
        parts = line.split()
        if "dev" in parts:
            idx = parts.index("dev")
            if idx + 1 < len(parts):
                return parts[idx + 1]
    return ""


def _collect_health_checks(settings: Settings) -> dict[str, bool]:
    xhttp_mode = settings.user_transport_mode.lower() == "xhttp"
    return {
        "xray_config_exists": os.path.exists(settings.xray_config),
        "xray_cert_exists": True if not xhttp_mode else os.path.exists("/usr/local/etc/xray/fullchain.crt"),
        "xray_key_exists": True if not xhttp_mode else os.path.exists("/usr/local/etc/xray/private.key"),
        "xray_active": _xray_is_active(settings.xray_service),
        "xray_listening_user_port": _xray_listens_port(settings.user_port),
    }


def _check_tcp_connectivity(host: str, port: int, timeout_seconds: float = 2.0) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout_seconds):
            return True
    except OSError:
        return False


def _get_tc_diagnostics(settings: Settings) -> dict[str, object]:
    iface = settings.limited_tc_egress_iface or _detect_default_iface()
    service_state = _service_state(settings.limited_tc_service)

    if not settings.limited_tc_enabled:
        return {
            "enabled": False,
            "service_state": service_state,
            "iface": iface,
            "mark": settings.limited_tc_mark,
            "class_id": settings.limited_tc_class_id,
            "qdisc_present": False,
            "filter_present": False,
        }

    if not iface:
        return {
            "enabled": True,
            "service_state": service_state,
            "iface": "",
            "mark": settings.limited_tc_mark,
            "class_id": settings.limited_tc_class_id,
            "qdisc_present": False,
            "filter_present": False,
        }

    qdisc_result = _run_command(["tc", "qdisc", "show", "dev", iface], timeout=3)
    filter_result = _run_command(["tc", "filter", "show", "dev", iface, "parent", "1:"], timeout=3)
    qdisc_text = "" if qdisc_result is None else qdisc_result.stdout
    filter_text = "" if filter_result is None else filter_result.stdout

    return {
        "enabled": True,
        "service_state": service_state,
        "iface": iface,
        "mark": settings.limited_tc_mark,
        "class_id": settings.limited_tc_class_id,
        "qdisc_present": "htb" in qdisc_text,
        "filter_present": settings.limited_tc_class_id in filter_text,
    }


def _get_ufw_diagnostics(settings: Settings) -> dict[str, object]:
    result = _run_command(["ufw", "status"], timeout=3)
    status_line = "unknown"
    active = False
    if result is not None and result.returncode == 0:
        status_line = (result.stdout.splitlines() or ["unknown"])[0]
        active = "Status: active" in result.stdout

    allow_from = [item.strip() for item in settings.api_allow_from.split(",") if item.strip()]
    api_exposure = "local-only"
    if settings.api_public:
        api_exposure = "allow-listed" if allow_from else "open"

    return {
        "ufw_active": active,
        "status_line": status_line,
        "api_exposure": api_exposure,
        "api_allow_from": allow_from,
    }


@app.on_event("startup")
def startup() -> None:
    global traffic_collector, enforcement_loop
    init_db()
    settings = get_settings()
    traffic_collector = TrafficCollector(settings=settings, interval_seconds=settings.limit_poll_interval_seconds)
    traffic_collector.start()
    enforcement_loop = EnforcementLoop(settings=settings, interval_seconds=settings.limit_poll_interval_seconds)
    enforcement_loop.start()


@app.on_event("shutdown")
def shutdown() -> None:
    global traffic_collector, enforcement_loop
    if enforcement_loop is not None:
        enforcement_loop.stop()
        enforcement_loop = None
    if traffic_collector is not None:
        traffic_collector.stop()
        traffic_collector = None


@app.get("/health")
def health(settings: Settings = Depends(get_settings)) -> JSONResponse:
    checks = _collect_health_checks(settings)
    ok = all(checks.values())
    return JSONResponse(status_code=200 if ok else 503, content={"status": "ok" if ok else "degraded", "checks": checks})


@app.get("/healthz", response_class=PlainTextResponse)
def healthz(settings: Settings = Depends(get_settings)) -> PlainTextResponse:
    checks = _collect_health_checks(settings)
    ok = all(checks.values())
    return PlainTextResponse("OK" if ok else "DEGRADED", status_code=200 if ok else 503)


@app.get("/v1/system/diagnostics")
def system_diagnostics(
    _: None = Depends(require_token),
    settings: Settings = Depends(get_settings),
) -> dict[str, object]:
    checks = _collect_health_checks(settings)
    tc_diag = _get_tc_diagnostics(settings)
    allow_from = [item.strip() for item in settings.api_allow_from.split(",") if item.strip()]

    return {
        "bridge_domain": settings.bridge_domain,
        "health": {
            "status": "ok" if all(checks.values()) else "degraded",
            "checks": checks,
        },
        "api": {
            "bind": settings.api_bind,
            "port": settings.api_port,
            "public": settings.api_public,
            "allow_from": allow_from,
        },
        "transport": {
            "mode": settings.user_transport_mode,
            "port": settings.user_port,
            "flow": settings.user_flow,
            "path": settings.user_path if settings.user_transport_mode.lower() == "xhttp" else None,
        },
        "reality": {
            "enabled": settings.user_transport_mode.lower() == "reality",
            "profile": settings.reality_profile,
            "effective_profile": settings.reality_effective_profile,
            "profile_source": settings.reality_profile_source,
            "server_name": settings.reality_server_name,
            "dest": settings.reality_dest,
            "fingerprint": settings.reality_fingerprint,
            "spider_x": settings.reality_spider_x,
            "has_public_key": bool(settings.reality_public_key),
            "has_short_id": bool(settings.reality_short_id),
        },
        "exit": {
            "host": settings.exit_host,
            "port": settings.exit_port,
            "path": settings.exit_path,
            "server_name": settings.exit_server_name,
            "has_bridge_uuid": bool(settings.bridge_uuid_for_exit),
            "reachable_tcp": _check_tcp_connectivity(settings.exit_host, settings.exit_port),
        },
        "services": {
            "xray": {
                "state": _service_state(settings.xray_service),
                "listening_user_port": _xray_listens_port(settings.user_port),
            },
            "bridge_manager": {
                "state": _service_state("bridge-manager"),
            },
            "tc": {
                "state": tc_diag["service_state"],
            },
        },
        "firewall": _get_ufw_diagnostics(settings),
        "traffic_shaping": tc_diag,
    }


@app.post("/v1/users")
def create_user(
    payload: CreateUserRequest,
    _: None = Depends(require_token),
    settings: Settings = Depends(get_settings),
) -> dict:
    session = get_session()
    try:
        user = session.get(User, payload.user_id)
        if user is not None and user.revoked_at is None:
            return _serialize_user(settings, user)

        new_uuid = str(uuid4())
        now = datetime.now(timezone.utc)

        if user is None:
            user = User(
                user_id=payload.user_id,
                uuid=new_uuid,
                label=payload.label,
                created_at=now,
                revoked_at=None,
            )
        else:
            user.uuid = new_uuid
            user.label = payload.label
            user.created_at = now
            user.revoked_at = None

        session.add(user)

        try:
            with traffic_state_guard():
                try:
                    # Save current runtime counters before xray restart inside config mutation.
                    persist_all_user_runtime_totals(settings)
                except Exception as exc:
                    LOG.warning("traffic snapshot before create_user failed: %s", exc)

                upsert_user_client(settings, payload.user_id, new_uuid)
        except XrayConfigError as exc:
            session.rollback()
            raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=str(exc)) from exc

        session.commit()
        session.refresh(user)
        return _serialize_user(settings, user)
    finally:
        session.close()


@app.delete("/v1/users/{user_id}")
def delete_user(
    user_id: str,
    _: None = Depends(require_token),
    settings: Settings = Depends(get_settings),
) -> dict:
    session = get_session()
    try:
        user = session.get(User, user_id)
        if user is None or user.revoked_at is not None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="User not found")

        try:
            with traffic_state_guard():
                try:
                    # Save current runtime counters before xray restart inside config mutation.
                    persist_all_user_runtime_totals(settings)
                except Exception as exc:
                    LOG.warning("traffic snapshot before delete_user failed: %s", exc)

                removed = remove_user_client(settings, user_id)
        except XrayConfigError as exc:
            session.rollback()
            raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=str(exc)) from exc

        user.revoked_at = datetime.now(timezone.utc)
        session.add(user)
        session.commit()

        return {"status": "deleted", "user_id": user_id, "removed_from_xray": removed}
    finally:
        session.close()


@app.get("/v1/users/{user_id}")
def get_user(
    user_id: str,
    _: None = Depends(require_token),
    settings: Settings = Depends(get_settings),
) -> dict:
    session = get_session()
    try:
        user = session.get(User, user_id)
        if user is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="User not found")
        return _serialize_user(settings, user)
    finally:
        session.close()


@app.get("/v1/users/{user_id}/traffic")
def user_traffic(
    user_id: str,
    _: None = Depends(require_token),
    settings: Settings = Depends(get_settings),
) -> dict:
    session = get_session()
    try:
        user = session.get(User, user_id)
        if user is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="User not found")
    finally:
        session.close()

    traffic = get_user_traffic(settings, user_id)
    return {"user_id": user_id, **traffic}


@app.get("/v1/users/{user_id}/limit-policy")
def get_limit_policy(
    user_id: str,
    _: None = Depends(require_token),
    settings: Settings = Depends(get_settings),
) -> dict:
    session = get_session()
    try:
        user = session.get(User, user_id)
        if user is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="User not found")

        policy = session.get(UserLimitPolicy, user_id)
        if policy is None:
            traffic = session.get(UserTraffic, user_id)
            total_bytes = (traffic.total_uplink + traffic.total_downlink) if traffic else 0
            return {
                "user_id": user_id,
                "mode": "unlimited",
                "traffic_limit_bytes": None,
                "post_limit_action": None,
                "throttle_rate_bytes_per_sec": None,
                "enforcement_state": "none",
                "limit_reached_at": None,
                "total_bytes_observed": total_bytes,
            }

        return {"user_id": user_id, **_serialize_policy(policy, session)}
    finally:
        session.close()


@app.put("/v1/users/{user_id}/limit-policy")
def set_limit_policy(
    user_id: str,
    payload: LimitPolicyRequest,
    _: None = Depends(require_token),
    settings: Settings = Depends(get_settings),
) -> dict:
    session = get_session()
    try:
        user = session.get(User, user_id)
        if user is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="User not found")

        policy = session.get(UserLimitPolicy, user_id)
        if policy is None:
            policy = UserLimitPolicy(
                user_id=user_id,
                mode="unlimited",
                enforcement_state="none",
                throttle_rate_bytes_per_sec=settings.limited_throttle_rate_bytes_per_sec,
            )

        old_mode = policy.mode
        old_enforcement = policy.enforcement_state

        policy.mode = payload.mode
        policy.traffic_limit_bytes = payload.traffic_limit_bytes
        policy.post_limit_action = payload.post_limit_action
        if payload.throttle_rate_bytes_per_sec is not None:
            policy.throttle_rate_bytes_per_sec = payload.throttle_rate_bytes_per_sec
        elif payload.mode == "limited" and payload.post_limit_action == "throttle":
            policy.throttle_rate_bytes_per_sec = settings.limited_throttle_rate_bytes_per_sec

        need_clear = False
        if payload.mode == "unlimited" and old_enforcement != "none":
            policy.enforcement_state = "none"
            policy.limit_reached_at = None
            policy.last_enforced_at = None
            need_clear = True

        session.add(policy)
        session.commit()
        session.refresh(policy)

        LOG.info("policy_changed user_id=%s old_mode=%s new_mode=%s old_enforcement=%s new_enforcement=%s",
                 user_id, old_mode, payload.mode, old_enforcement, policy.enforcement_state)

        if need_clear:
            try:
                reapply_enforcement_routing(settings)
                LOG.info("enforcement_cleared user_id=%s", user_id)
            except Exception as exc:
                LOG.warning("enforcement clear xray reload failed: %s", exc)

        result = {"user_id": user_id, **_serialize_policy(policy, session)}
    finally:
        session.close()

    return result
