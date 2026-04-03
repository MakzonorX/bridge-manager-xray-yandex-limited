from datetime import datetime, timezone
from typing import Literal, Optional
from uuid import uuid4
from urllib.parse import quote
import logging

from fastapi import Depends, FastAPI, HTTPException, status
from fastapi.responses import JSONResponse
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
    import subprocess

    result = subprocess.run(["systemctl", "is-active", service_name], capture_output=True, text=True)
    return result.returncode == 0 and result.stdout.strip() == "active"


def _xray_listens_port(port: int) -> bool:
    import subprocess

    result = subprocess.run(["ss", "-lnt"], capture_output=True, text=True)
    if result.returncode != 0:
        return False
    return f":{port}" in result.stdout


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
    xhttp_mode = settings.user_transport_mode.lower() == "xhttp"
    checks = {
        "xray_config_exists": __import__("os").path.exists(settings.xray_config),
        "xray_cert_exists": True if not xhttp_mode else __import__("os").path.exists("/usr/local/etc/xray/fullchain.crt"),
        "xray_key_exists": True if not xhttp_mode else __import__("os").path.exists("/usr/local/etc/xray/private.key"),
        "xray_active": _xray_is_active(settings.xray_service),
        "xray_listening_user_port": _xray_listens_port(settings.user_port),
    }
    ok = all(checks.values())
    return JSONResponse(status_code=200 if ok else 503, content={"status": "ok" if ok else "degraded", "checks": checks})


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
