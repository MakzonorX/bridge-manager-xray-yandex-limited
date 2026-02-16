from pathlib import Path

from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from .models import Base
from .settings import get_settings

_engine = None
_SessionLocal = None


def init_db() -> None:
    global _engine, _SessionLocal

    settings = get_settings()
    Path(settings.db_path).parent.mkdir(parents=True, exist_ok=True)

    _engine = create_engine(
        f"sqlite:///{settings.db_path}",
        connect_args={"check_same_thread": False},
        future=True,
    )
    _SessionLocal = sessionmaker(bind=_engine, autoflush=False, autocommit=False, future=True)
    Base.metadata.create_all(bind=_engine)


def get_session() -> Session:
    if _SessionLocal is None:
        init_db()

    assert _SessionLocal is not None
    return _SessionLocal()
