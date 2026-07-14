import enum
from datetime import datetime

from sqlalchemy import BigInteger, Boolean, DateTime, Enum, ForeignKey, String, Text, func
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    pass


class RegistrationState(str, enum.Enum):
    NEW = "new"
    AWAITING_CONSENT = "awaiting_consent"
    AWAITING_FIO = "awaiting_fio"
    AWAITING_ADDRESS = "awaiting_address"
    AWAITING_PHONE = "awaiting_phone"
    COMPLETED = "completed"


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(primary_key=True)
    max_user_id: Mapped[int] = mapped_column(BigInteger, unique=True, index=True)
    max_username: Mapped[str | None] = mapped_column(String(255))
    max_name: Mapped[str | None] = mapped_column(String(255))

    state: Mapped[RegistrationState] = mapped_column(
        Enum(RegistrationState), default=RegistrationState.NEW
    )

    fio: Mapped[str | None] = mapped_column(String(500))
    address: Mapped[str | None] = mapped_column(Text)
    phone: Mapped[str | None] = mapped_column(String(32), index=True)
    phone_verified: Mapped[bool] = mapped_column(Boolean, default=False)

    consent_given: Mapped[bool] = mapped_column(Boolean, default=False)
    consent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    is_registered: Mapped[bool] = mapped_column(Boolean, default=False, index=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
    registered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    screenshots: Mapped[list["Screenshot"]] = relationship(back_populates="user")
    broadcast_deliveries: Mapped[list["BroadcastDelivery"]] = relationship(
        back_populates="user"
    )


class Broadcast(Base):
    __tablename__ = "broadcasts"

    id: Mapped[int] = mapped_column(primary_key=True)
    text: Mapped[str] = mapped_column(Text)
    request_screenshot: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    total_recipients: Mapped[int] = mapped_column(default=0)
    sent_count: Mapped[int] = mapped_column(default=0)
    failed_count: Mapped[int] = mapped_column(default=0)

    deliveries: Mapped[list["BroadcastDelivery"]] = relationship(back_populates="broadcast")


class BroadcastDelivery(Base):
    __tablename__ = "broadcast_deliveries"

    id: Mapped[int] = mapped_column(primary_key=True)
    broadcast_id: Mapped[int] = mapped_column(ForeignKey("broadcasts.id"), index=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    status: Mapped[str] = mapped_column(String(32), default="pending")
    error_message: Mapped[str | None] = mapped_column(Text)
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    broadcast: Mapped["Broadcast"] = relationship(back_populates="deliveries")
    user: Mapped["User"] = relationship(back_populates="broadcast_deliveries")


class Screenshot(Base):
    __tablename__ = "screenshots"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    broadcast_id: Mapped[int | None] = mapped_column(ForeignKey("broadcasts.id"), index=True)
    file_url: Mapped[str | None] = mapped_column(Text)
    file_token: Mapped[str | None] = mapped_column(String(512))
    message_id: Mapped[str | None] = mapped_column(String(128))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    user: Mapped["User"] = relationship(back_populates="screenshots")
