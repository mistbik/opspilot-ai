
from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import Boolean, CheckConstraint, ForeignKey, Index, Integer, JSON, String
from sqlalchemy import UniqueConstraint, Uuid
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column
from sqlalchemy import Text, DateTime, func


# Base class for all database models
class Base(DeclarativeBase):
    pass


class Event(Base):

    __tablename__ = "events"

    id: Mapped[int] = mapped_column(
        Integer,
        primary_key=True,
        index=True
    )

    event_id: Mapped[str] = mapped_column(
        String(255),
        unique=True,
        nullable=False
    )

    event_type: Mapped[str] = mapped_column(
        String(100),
        nullable=False
    )

    payload: Mapped[dict[str, Any]] = mapped_column(
        JSON,
        nullable=False
    )


class QuarantinedRecord(Base):

    __tablename__ = "quarantined_records"

    id: Mapped[int] = mapped_column(
        Integer,
        primary_key=True
    )

    row_number: Mapped[int] = mapped_column(
        Integer,
        nullable=False
    )

    raw_data: Mapped[str] = mapped_column(
        Text,
        nullable=False
    )

    error_reason: Mapped[str] = mapped_column(
        Text,
        nullable=False
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now()
    )


class Endpoint(Base):
    __tablename__ = "endpoints"

    id: Mapped[int] = mapped_column(primary_key=True)
    url: Mapped[str] = mapped_column(Text)
    signing_secret: Mapped[str] = mapped_column(Text)
    enabled: Mapped[bool] = mapped_column(Boolean, server_default="true")


class Delivery(Base):
    __tablename__ = "deliveries"
    __table_args__ = (
        UniqueConstraint("event_id", "endpoint_id"),
        CheckConstraint("status IN ('pending', 'delivered', 'dead_lettered')"),
        Index("ix_deliveries_poll", "status", "next_attempt_at", "id"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    event_id: Mapped[int] = mapped_column(ForeignKey("events.id"))
    endpoint_id: Mapped[int] = mapped_column(ForeignKey("endpoints.id"), index=True)
    idempotency_key: Mapped[UUID] = mapped_column(Uuid, unique=True, server_default=func.gen_random_uuid())
    status: Mapped[str] = mapped_column(String(20), server_default="pending")
    attempt_count: Mapped[int] = mapped_column(server_default="0")
    next_attempt_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class DeliveryAttempt(Base):
    __tablename__ = "delivery_attempts"
    __table_args__ = (UniqueConstraint("delivery_id", "attempt_number"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    delivery_id: Mapped[int] = mapped_column(ForeignKey("deliveries.id"))
    attempt_number: Mapped[int]
    attempted_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    status_code: Mapped[int | None]
    outcome: Mapped[str] = mapped_column(String(32))


class DeadLetterEvent(Base):
    __tablename__ = "dead_letter_events"

    delivery_id: Mapped[int] = mapped_column(ForeignKey("deliveries.id"), primary_key=True)
    reason: Mapped[str] = mapped_column(String(32))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class InvestigationScenario(Base):
    __tablename__ = "investigation_scenarios"

    name: Mapped[str] = mapped_column(String(40), primary_key=True)
    endpoint_id: Mapped[int] = mapped_column(ForeignKey("endpoints.id"), unique=True)
    delivery_id: Mapped[int] = mapped_column(ForeignKey("deliveries.id"), unique=True)
    expected_status: Mapped[int]
    expected_root_cause: Mapped[str] = mapped_column(String(100))


class RetryApproval(Base):
    __tablename__ = "retry_approvals"
    __table_args__ = (UniqueConstraint("delivery_id", "attempt_number"),)

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, server_default=func.gen_random_uuid())
    delivery_id: Mapped[int] = mapped_column(ForeignKey("deliveries.id"), index=True)
    action: Mapped[str] = mapped_column(String(40))
    arguments: Mapped[dict[str, int]] = mapped_column(JSON)
    arguments_hash: Mapped[str] = mapped_column(String(64))
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    approved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    attempt_number: Mapped[int | None]
