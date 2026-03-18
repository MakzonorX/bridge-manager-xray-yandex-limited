from datetime import datetime, timezone
from uuid import uuid4
from urllib.parse import quote
import logging

from fastapi import Depends, FastAPI, HTTPException, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from .auth import require_token
from .models import User
from .settings import Settings, get_settings
from .stats import TrafficCollector, get_user_traffic, persist_all_user_runtime_totals, traffic_state_guard
from .storage import get_session, init_db
from .xray_config import XrayConfigError, remove_user_client, upsert_user_client

app = FastAPI(title="Bridge Manager", version="1.0.0")
LOG = logging.getLogger(__name__)
traffic_collector: TrafficCollector | None = None


class CreateUserRequest(BaseModel):
    user_id: str = Field(min_length=1, max_length=128)
    label: str | None = Field(default=None, max_length=255)


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
    return payload


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
    global traffic_collector
    init_db()
    settings = get_settings()
    traffic_collector = TrafficCollector(settings=settings, interval_seconds=15)
    traffic_collector.start()


@app.on_event("shutdown")
def shutdown() -> None:
    global traffic_collector
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
