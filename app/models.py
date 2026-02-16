from datetime import datetime

from sqlalchemy import DateTime, String
from sqlalchemy.orm import Mapped, declarative_base, mapped_column


Base = declarative_base()


class User(Base):
    __tablename__ = "users"

    user_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    uuid: Mapped[str] = mapped_column(String(36), nullable=False)
    label: Mapped[str | None] = mapped_column(String(255), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
