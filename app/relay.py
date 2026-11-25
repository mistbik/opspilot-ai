import json
from datetime import timedelta
from random import uniform

import httpx
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import DeadLetterEvent, Delivery, DeliveryAttempt, Endpoint, Event, RetryApproval
from app.security import sign_payload

MAX_ATTEMPTS = 5
HTTP_TIMEOUT_SECONDS = 5.0
ATTEMPT_LEASE_SECONDS = 30
RETRYABLE_OUTCOMES = {"timeout", "transport_error", "retryable_http_status"}


def retry_delay(attempt_number: int) -> timedelta:
    base_seconds = 5 * 2 ** (attempt_number - 1)
    return timedelta(seconds=base_seconds * uniform(0.8, 1.2))


def mark_dead_letter(session: Session, delivery: Delivery, reason: str) -> None:
    delivery.status = "dead_lettered"
    dead_letter = session.get(DeadLetterEvent, delivery.id)
    if dead_letter is None:
        session.add(DeadLetterEvent(delivery_id=delivery.id, reason=reason))
    else:
        dead_letter.reason = reason


def endpoint_url(value: str, *, allow_loopback_http: bool = False) -> httpx.URL:
    url = httpx.URL(value)
    loopback_http = (
        allow_loopback_http and url.scheme == "http" and url.host in ("127.0.0.1", "::1")
    )
    if (url.scheme != "https" and not loopback_http) or not url.host or url.userinfo or url.fragment:
        raise ValueError("Use an HTTPS endpoint URL without user info or a fragment")
    return url


def send_delivery(
    client: httpx.Client, endpoint: Endpoint, event: Event, delivery: Delivery,
    *, allow_loopback_http: bool = False,
) -> tuple[str, int | None]:
    if not endpoint.enabled:
        return "endpoint_disabled", None
    try:
        url = endpoint_url(endpoint.url, allow_loopback_http=allow_loopback_http)
    except (ValueError, httpx.InvalidURL):
        return "invalid_endpoint", None

    body = json.dumps({
        "event_id": event.event_id,
        "event_type": event.event_type,
        "payload": event.payload,
    }, separators=(",", ":"), allow_nan=False).encode("utf-8")
    headers = {
        "Content-Type": "application/json",
        "X-Signature": sign_payload(body, endpoint.signing_secret),
        "Idempotency-Key": str(delivery.idempotency_key),
    }
    try:
        with client.stream("POST", url, content=body, headers=headers, follow_redirects=False) as response:
            code = response.status_code
    except httpx.TimeoutException:
        return "timeout", None
    except httpx.RequestError:
        return "transport_error", None

    if 200 <= code < 300:
        return "delivered", code
    if code in (408, 429) or 500 <= code < 600:
        return "retryable_http_status", code
    return "permanent_http_status", code


def process_next_delivery(
    session: Session, client: httpx.Client, *, allow_loopback_http: bool = False
) -> bool:
    with session.begin():
        delivery = session.scalars(
            select(Delivery)
            .where(Delivery.status == "pending", Delivery.next_attempt_at <= func.now())
            .order_by(Delivery.next_attempt_at, Delivery.id)
            .limit(1)
            .with_for_update(skip_locked=True)
        ).first()
        if delivery is None:
            return False

        attempt_number = delivery.attempt_count + 1
        authorized_attempt = session.scalar(select(RetryApproval.attempt_number).where(
            RetryApproval.delivery_id == delivery.id,
            RetryApproval.approved_at.is_not(None),
            RetryApproval.attempt_number.in_((delivery.attempt_count, attempt_number)),
        ).order_by(RetryApproval.attempt_number.desc()).limit(1))
        manual_retry = authorized_attempt == attempt_number
        previous_attempt = session.scalar(select(DeliveryAttempt).where(
            DeliveryAttempt.delivery_id == delivery.id,
            DeliveryAttempt.attempt_number == delivery.attempt_count,
            DeliveryAttempt.outcome == "in_progress",
        ))
        if previous_attempt is not None:
            previous_attempt.outcome = "interrupted"
        if not manual_retry and (
            delivery.attempt_count >= MAX_ATTEMPTS or authorized_attempt == delivery.attempt_count
        ):
            mark_dead_letter(session, delivery, "interrupted")
            return True

        delivery_id = delivery.id
        delivery.attempt_count = attempt_number
        delivery.next_attempt_at = func.clock_timestamp() + timedelta(seconds=ATTEMPT_LEASE_SECONDS)
        attempt = DeliveryAttempt(
            delivery_id=delivery_id,
            attempt_number=attempt_number,
            attempted_at=func.clock_timestamp(),
            outcome="in_progress",
        )
        session.add(attempt)
        session.flush()
        attempt_id = attempt.id

    # Commit the attempt before doing HTTP. After a crash the lease expires and another
    # worker can recover it. The second row lock prevents overlap during a slow send.
    with session.begin():
        delivery = session.get(Delivery, delivery_id, with_for_update={"skip_locked": True})
        if delivery is None or delivery.status != "pending" or delivery.attempt_count != attempt_number:
            return True

        event = session.get_one(Event, delivery.event_id)
        endpoint = session.get_one(Endpoint, delivery.endpoint_id)
        outcome, code = send_delivery(
            client, endpoint, event, delivery, allow_loopback_http=allow_loopback_http
        )
        attempt = session.get_one(DeliveryAttempt, attempt_id)
        attempt.outcome = outcome
        attempt.status_code = code

        if outcome == "delivered":
            delivery.status = "delivered"
        elif not manual_retry and outcome in RETRYABLE_OUTCOMES and attempt_number < MAX_ATTEMPTS:
            delivery.next_attempt_at = func.clock_timestamp() + retry_delay(attempt_number)
        else:
            mark_dead_letter(session, delivery, outcome)
    return True
