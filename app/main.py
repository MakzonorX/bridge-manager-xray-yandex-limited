from datetime import datetime, timezone
from uuid import uuid4
from urllib.parse import quote

from fastapi import Depends, FastAPI, HTTPException, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from .auth import require_token
from .models import User
from .settings import Settings, get_settings
from .stats import get_user_traffic
from .storage import get_session, init_db
from .xray_config import XrayConfigError, remove_user_client, upsert_user_client

app = FastAPI(title="Bridge Manager", version="1.0.0")


class CreateUserRequest(BaseModel):
    user_id: str = Field(min_length=1, max_length=128)
    label: str | None = Field(default=None, max_length=255)


def _build_vless_uri(settings: Settings, user_uuid: str, user_id: str, label: str | None) -> str:
    fragment = quote(label or user_id, safe="")
    encoded_path = quote(settings.user_path, safe="")
    return (
        f"vless://{user_uuid}@{settings.bridge_domain}:{settings.user_port}"
        f"?encryption=none&security=tls&sni={settings.bridge_domain}&type=xhttp&path={encoded_path}"
        f"#{fragment}"
    )


def _serialize_user(settings: Settings, user: User) -> dict:
    return {
        "user_id": user.user_id,
        "uuid": user.uuid,
        "label": user.label,
        "created_at": user.created_at.isoformat(),
        "revoked_at": user.revoked_at.isoformat() if user.revoked_at else None,
        "active": user.revoked_at is None,
        "vless_uri": _build_vless_uri(settings, user.uuid, user.user_id, user.label),
    }


def _xray_is_active(service_name: str) -> bool:
    import subprocess

    result = subprocess.run(["systemctl", "is-active", service_name], capture_output=True, text=True)
    return result.returncode == 0 and result.stdout.strip() == "active"


def _xray_listens_443() -> bool:
    import subprocess

    result = subprocess.run(["ss", "-lnt"], capture_output=True, text=True)
    if result.returncode != 0:
        return False
    return ":443" in result.stdout


@app.on_event("startup")
def startup() -> None:
    init_db()


@app.get("/health")
def health(settings: Settings = Depends(get_settings)) -> JSONResponse:
    checks = {
        "xray_config_exists": __import__("os").path.exists(settings.xray_config),
        "xray_cert_exists": __import__("os").path.exists("/usr/local/etc/xray/fullchain.crt"),
        "xray_key_exists": __import__("os").path.exists("/usr/local/etc/xray/private.key"),
        "xray_active": _xray_is_active(settings.xray_service),
        "xray_listening_443": _xray_listens_443(),
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
