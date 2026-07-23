import enum
import json
from datetime import datetime

from sqlalchemy import BigInteger, Boolean, DateTime, Enum, ForeignKey, Integer, String, Text, func
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    pass


class RegistrationState(str, enum.Enum):
    NEW = "NEW"
    AWAITING_CONSENT = "AWAITING_CONSENT"
    AWAITING_FIO = "AWAITING_FIO"
    AWAITING_BIRTH_DATE = "AWAITING_BIRTH_DATE"
    AWAITING_ADDRESS = "AWAITING_ADDRESS"
    AWAITING_ADDRESS_CONFIRM = "awaiting_address_confirm"
    AWAITING_PHONE = "AWAITING_PHONE"
    AWAITING_PHONE_MANUAL = "awaiting_phone_manual"
    COMPLETED = "COMPLETED"


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
    birth_date: Mapped[str | None] = mapped_column(String(10))
    address: Mapped[str | None] = mapped_column(Text)
    address_normalized: Mapped[str | None] = mapped_column(Text)
    district: Mapped[str | None] = mapped_column(String(255), index=True)
    region: Mapped[str | None] = mapped_column(String(255))
    city: Mapped[str | None] = mapped_column(String(255))
    settlement: Mapped[str | None] = mapped_column(String(255))
    address_qc: Mapped[int | None] = mapped_column(Integer)

    phone: Mapped[str | None] = mapped_column(String(32), index=True)
    phone_verified: Mapped[bool] = mapped_column(Boolean, default=False)

    appeal_flow_state: Mapped[str | None] = mapped_column(String(16), default="none")
    appeal_draft_subject: Mapped[str | None] = mapped_column(String(200))
    appeal_draft_message: Mapped[str | None] = mapped_column(Text)
    appeal_draft_attachments: Mapped[str | None] = mapped_column(Text)

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
    districts: Mapped[str | None] = mapped_column(Text)
    image_token: Mapped[str | None] = mapped_column(String(512))
    image_url: Mapped[str | None] = mapped_column(Text)
    image_filename: Mapped[str | None] = mapped_column(String(255))
    source_chat_id: Mapped[int | None] = mapped_column(BigInteger, index=True)
    source_message_id: Mapped[str | None] = mapped_column(String(128), unique=True)
    auto_from_channel: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    total_recipients: Mapped[int] = mapped_column(default=0)
    sent_count: Mapped[int] = mapped_column(default=0)
    failed_count: Mapped[int] = mapped_column(default=0)

    deliveries: Mapped[list["BroadcastDelivery"]] = relationship(back_populates="broadcast")

    def get_district_list(self) -> list[str]:
        if not self.districts:
            return []
        try:
            data = json.loads(self.districts)
            return [str(x) for x in data if x]
        except json.JSONDecodeError:
            return []


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


class AdminRole(str, enum.Enum):
    BROADCASTER = "broadcaster"


class NewsChannelSource(Base):
    __tablename__ = "news_channel_sources"

    id: Mapped[int] = mapped_column(primary_key=True)
    channel_slug: Mapped[str] = mapped_column(String(128), default="OErohanovaSP")
    channel_url: Mapped[str] = mapped_column(
        String(255), default="https://max.ru/OErohanovaSP"
    )
    title: Mapped[str | None] = mapped_column(String(255))
    chat_id: Mapped[int | None] = mapped_column(BigInteger, unique=True)
    is_enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class AdminUser(Base):
    __tablename__ = "admin_users"

    id: Mapped[int] = mapped_column(primary_key=True)
    username: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    password_hash: Mapped[str] = mapped_column(String(255))
    display_name: Mapped[str | None] = mapped_column(String(255))
    role: Mapped[AdminRole] = mapped_column(Enum(AdminRole), default=AdminRole.BROADCASTER)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


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
