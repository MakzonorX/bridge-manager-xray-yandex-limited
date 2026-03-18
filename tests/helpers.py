from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path

import app.storage as storage
from app.settings import Settings


def make_settings(tmp_path: Path) -> Settings:
    return Settings(
        bridge_domain="bridge.test",
        api_token="test-token",
        db_path=str(tmp_path / "bridge_manager.db"),
        xray_config=str(tmp_path / "config.json"),
        xray_bin="/usr/local/bin/xray",
    )


@contextmanager
def isolated_storage(settings: Settings):
    original_get_settings = storage.get_settings
    original_engine = storage._engine
    original_session_local = storage._SessionLocal

    if storage._engine is not None:
        storage._engine.dispose()

    storage._engine = None
    storage._SessionLocal = None
    storage.get_settings = lambda: settings
    storage.init_db()

    try:
        yield
    finally:
        if storage._engine is not None:
            storage._engine.dispose()
        storage._engine = original_engine
        storage._SessionLocal = original_session_local
        storage.get_settings = original_get_settings
