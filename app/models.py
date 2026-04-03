from datetime import datetime

from sqlalchemy import BigInteger, DateTime, String
from sqlalchemy.orm import Mapped, declarative_base, mapped_column


Base = declarative_base()


class User(Base):
    __tablename__ = "users"

    user_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    uuid: Mapped[str] = mapped_column(String(36), nullable=False)
    label: Mapped[str | None] = mapped_column(String(255), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class UserTraffic(Base):
    __tablename__ = "user_traffic"

    user_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    total_uplink: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    total_downlink: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    last_runtime_uplink: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    last_runtime_downlink: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class UserLimitPolicy(Base):
    __tablename__ = "user_limit_policies"

    user_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    mode: Mapped[str] = mapped_column(String(16), nullable=False, default="unlimited")
    traffic_limit_bytes: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    post_limit_action: Mapped[str | None] = mapped_column(String(16), nullable=True)
    throttle_rate_bytes_per_sec: Mapped[int] = mapped_column(BigInteger, nullable=False, default=102400)
    enforcement_state: Mapped[str] = mapped_column(String(16), nullable=False, default="none")
    limit_reached_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_enforced_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
