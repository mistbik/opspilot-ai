import hashlib
import hmac
import json
from datetime import timedelta
from typing import Literal
from uuid import UUID

from fastapi import HTTPException
from pydantic import BaseModel, ConfigDict, PositiveInt
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import Delivery, DeliveryAttempt, Endpoint, RetryApproval

APPROVAL_TTL = timedelta(minutes=15)
RETRY_ACTION = "retry_delivery"


class RetryArguments(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    delivery_id: PositiveInt
    expected_attempt_count: PositiveInt


class RetryAction(BaseModel):
    model_config = ConfigDict(extra="forbid")

    action: Literal["retry_delivery"]
    arguments: RetryArguments


def arguments_hash(arguments: dict[str, int]) -> str:
    canonical_json = json.dumps(arguments, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical_json.encode()).hexdigest()


def database_time(session: Session):
    return session.scalar(select(func.clock_timestamp()))


def request_retry_approval(session: Session, delivery_id: int) -> RetryApproval:
    delivery = session.get(Delivery, delivery_id, with_for_update=True)
    if delivery is None:
        raise HTTPException(404, "Delivery not found")
    endpoint = session.get_one(Endpoint, delivery.endpoint_id)
    if delivery.status != "dead_lettered" or not endpoint.enabled or not delivery.attempt_count:
        raise HTTPException(409, "Only dead-lettered deliveries to enabled endpoints can be retried")

    arguments = {
        "delivery_id": delivery.id,
        "expected_attempt_count": delivery.attempt_count,
    }
    digest = arguments_hash(arguments)
    now = database_time(session)
    existing = session.scalars(select(RetryApproval).where(
        RetryApproval.delivery_id == delivery.id,
        RetryApproval.arguments_hash == digest,
        RetryApproval.approved_at.is_(None),
        RetryApproval.expires_at > now,
    )).first()
    if existing is not None:
        return existing
    approval = RetryApproval(
        delivery_id=delivery.id, action=RETRY_ACTION, arguments=arguments,
        arguments_hash=digest, expires_at=now + APPROVAL_TTL,
    )
    session.add(approval)
    session.flush()
    return approval


def approve_retry(session: Session, approval_id: UUID, requested: RetryAction) -> RetryApproval:
    approval = session.get(RetryApproval, approval_id, with_for_update=True)
    if approval is None:
        raise HTTPException(404, "Approval not found")
    if approval.approved_at is not None:
        raise HTTPException(409, "Approval already used")
    arguments = requested.arguments.model_dump()
    if (requested.action != approval.action or arguments != approval.arguments
            or not hmac.compare_digest(arguments_hash(arguments), approval.arguments_hash)
            or arguments["delivery_id"] != approval.delivery_id):
        raise HTTPException(409, "Action or arguments do not match the approval")

    delivery = session.get_one(Delivery, approval.delivery_id, with_for_update=True)
    now = database_time(session)
    if now >= approval.expires_at:
        raise HTTPException(410, "Approval expired")
    endpoint = session.get_one(Endpoint, delivery.endpoint_id)
    if (delivery.status != "dead_lettered" or not endpoint.enabled
            or delivery.attempt_count != arguments["expected_attempt_count"]):
        raise HTTPException(409, "Delivery changed since the approval was requested")

    approval.approved_at = now
    approval.attempt_number = delivery.attempt_count + 1
    delivery.status = "pending"
    delivery.next_attempt_at = now
    return approval


def approval_details(session: Session, approval: RetryApproval) -> dict:
    attempt = None
    if approval.attempt_number is not None:
        attempt = session.scalar(select(DeliveryAttempt).where(
            DeliveryAttempt.delivery_id == approval.delivery_id,
            DeliveryAttempt.attempt_number == approval.attempt_number,
        ))
    status = "awaiting_approval"
    result = None
    if approval.approved_at is not None:
        status = "queued"
        if attempt is not None:
            status = "in_progress" if attempt.outcome == "in_progress" else "completed"
            result = {
                "attempt_number": attempt.attempt_number, "outcome": attempt.outcome,
                "http_status": attempt.status_code,
                "delivered": attempt.outcome == "delivered" and (
                    attempt.status_code is not None and 200 <= attempt.status_code < 300
                ),
            }
    elif database_time(session) >= approval.expires_at:
        status = "expired"
    return {
        "approval_id": approval.id, "action": approval.action, "arguments": approval.arguments,
        "arguments_hash": approval.arguments_hash, "expires_at": approval.expires_at,
        "approved_at": approval.approved_at, "status": status, "result": result,
    }
