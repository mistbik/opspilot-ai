import hashlib
import hmac
import json
from concurrent.futures import ThreadPoolExecutor
from threading import Event as ThreadEvent

import httpx
import pytest
from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from app import relay, worker
from app.models import DeadLetterEvent, Delivery, DeliveryAttempt, Endpoint, Event
from app.relay import MAX_ATTEMPTS, process_next_delivery

OUTBOUND_SECRET = "pytest-outbound-signing-secret"
EVENT = {"event_id": "relay-1", "event_type": "order.created", "payload": {"amount": 5}}


def add_endpoints(engine, *enabled_flags):
    with Session(engine) as session, session.begin():
        for index, enabled in enumerate(enabled_flags):
            session.add(Endpoint(
                url=f"https://receiver.example/{index}", signing_secret=OUTBOUND_SECRET, enabled=enabled
            ))


def upload(client, *events):
    return client.post("/upload", files={"file": ("events.json", json.dumps(events))})


def process(engine, transport):
    with Session(engine) as session, httpx.Client(transport=transport) as http:
        return process_next_delivery(session, http)


def test_fanout_for_webhook_and_upload(postgres_client, test_engine, signing_secret):
    add_endpoints(test_engine, True, False, True)
    body = json.dumps(EVENT).encode()
    signature = "sha256=" + hmac.new(signing_secret.encode(), body, hashlib.sha256).hexdigest()
    headers = {"X-Signature": signature}
    assert postgres_client.post("/webhook", content=body, headers=headers).json()["status"] == "processed"
    assert postgres_client.post("/webhook", content=body, headers=headers).json()["status"] == "already_processed"
    result = upload(postgres_client, EVENT, {}, {**EVENT, "event_id": "relay-2"}).json()
    assert (result["accepted"], result["rejected"]) == (1, 2)

    with Session(test_engine) as session:
        deliveries = session.scalars(select(Delivery)).all()
        enabled_ids = set(session.scalars(select(Endpoint.id).where(Endpoint.enabled.is_(True))))
        event_ids = set(session.scalars(select(Event.id)))
        assert {(row.event_id, row.endpoint_id) for row in deliveries} == {
            (event_id, endpoint_id) for event_id in event_ids for endpoint_id in enabled_ids
        }
        assert len(deliveries) == len({row.idempotency_key for row in deliveries}) == 4
        assert all(row.status == "pending" for row in deliveries)


def test_batch_rollback_removes_delivery_records(postgres_client, test_engine):
    add_endpoints(test_engine, True)
    with test_engine.begin() as connection:
        connection.exec_driver_sql("ALTER TABLE quarantined_records ADD CHECK (row_number <> 2)")
    assert upload(postgres_client, EVENT, {}).status_code == 500
    with Session(test_engine) as session:
        assert session.scalars(select(Event)).all() == []
        assert session.scalars(select(Delivery)).all() == []


@pytest.mark.parametrize(("first_code", "first_outcome"), [
    (None, "timeout"), (429, "retryable_http_status"),
])
def test_worker_signs_retries_and_records_success(postgres_client, test_engine, first_code, first_outcome):
    add_endpoints(test_engine, True)
    assert upload(postgres_client, EVENT).status_code == 200
    requests = []

    def receiver(request):
        assert json.loads(request.content) == EVENT
        expected = hmac.new(OUTBOUND_SECRET.encode(), request.content, hashlib.sha256).hexdigest()
        assert request.headers["X-Signature"] == f"sha256={expected}"
        requests.append(request.headers["Idempotency-Key"])
        if len(requests) == 1:
            if first_code is None:
                raise httpx.ReadTimeout("test timeout", request=request)
            return httpx.Response(first_code, headers={"Retry-After": "10"})
        return httpx.Response(204)

    transport = httpx.MockTransport(receiver)
    assert process(test_engine, transport)
    assert not process(test_engine, transport)
    with Session(test_engine) as session, session.begin():
        session.execute(update(Delivery).values(next_attempt_at=func.now()))
    assert process(test_engine, transport)
    assert not process(test_engine, transport)

    with Session(test_engine) as session:
        delivery = session.scalars(select(Delivery)).one()
        assert requests == [str(delivery.idempotency_key)] * 2
        assert (delivery.status, delivery.attempt_count) == ("delivered", 2)
        attempts = session.scalars(select(DeliveryAttempt).order_by(DeliveryAttempt.attempt_number)).all()
        assert [(row.outcome, row.status_code) for row in attempts] == [
            (first_outcome, first_code), ("delivered", 204),
        ]


@pytest.mark.parametrize(("factor", "expected"), [
    (0.8, [4, 8, 16, 32]), (1.2, [6, 12, 24, 48]),
])
def test_retry_delay_grows_exponentially_with_bounded_jitter(monkeypatch, factor, expected):
    def choose(low, high):
        assert (low, high) == (0.8, 1.2)
        return factor

    monkeypatch.setattr(relay, "uniform", choose)
    assert [relay.retry_delay(n).total_seconds() for n in range(1, MAX_ATTEMPTS)] == expected


@pytest.mark.parametrize("crashes", [1, MAX_ATTEMPTS])
def test_interrupted_attempt_is_recovered_or_dead_lettered(postgres_client, test_engine, crashes):
    add_endpoints(test_engine, True)
    assert upload(postgres_client, EVENT).status_code == 200
    keys = []

    def interrupted(request):
        keys.append(request.headers["Idempotency-Key"])
        raise SystemExit("simulated worker exit after send")

    def recovered(request):
        keys.append(request.headers["Idempotency-Key"])
        return httpx.Response(200)

    recovery_transport = httpx.MockTransport(recovered)
    for _ in range(crashes):
        with pytest.raises(SystemExit):
            process(test_engine, httpx.MockTransport(interrupted))
        assert not process(test_engine, recovery_transport)
        with Session(test_engine) as session, session.begin():
            last = session.scalars(
                select(DeliveryAttempt).order_by(DeliveryAttempt.attempt_number.desc()).limit(1)
            ).one()
            assert last.outcome == "in_progress"
            session.execute(update(Delivery).values(next_attempt_at=func.now()))

    assert process(test_engine, recovery_transport)
    assert len(set(keys)) == 1
    with Session(test_engine) as session:
        delivery = session.scalar(select(Delivery))
        outcomes = session.scalars(
            select(DeliveryAttempt.outcome).order_by(DeliveryAttempt.attempt_number)
        ).all()
        assert outcomes[:crashes] == ["interrupted"] * crashes
        if crashes == MAX_ATTEMPTS:
            assert outcomes == ["interrupted"] * MAX_ATTEMPTS
            assert delivery.status == "dead_lettered"
            assert session.get_one(DeadLetterEvent, delivery.id).reason == "interrupted"
        else:
            assert outcomes == ["interrupted", "delivered"]
            assert delivery.status == "delivered"


def test_demo_registration_is_idempotent(postgres_client, test_engine, monkeypatch):
    monkeypatch.setattr(worker, "engine", test_engine)
    assert worker.check_database(require_mock=True) == 1
    worker.register_mock_endpoint(OUTBOUND_SECRET)
    worker.register_mock_endpoint(OUTBOUND_SECRET)
    assert worker.check_database(require_mock=True) == 0
    with Session(test_engine) as session:
        endpoint = session.scalars(select(Endpoint)).one()
        assert endpoint.url == worker.MOCK_ENDPOINT_URL
        assert endpoint.enabled


def test_worker_dead_letters_exhausted_delivery(postgres_client, test_engine):
    add_endpoints(test_engine, True)
    assert upload(postgres_client, EVENT).status_code == 200
    transport = httpx.MockTransport(lambda request: httpx.Response(503))
    for _ in range(MAX_ATTEMPTS):
        with Session(test_engine) as session, session.begin():
            session.execute(update(Delivery).values(next_attempt_at=func.now()))
        assert process(test_engine, transport)
    assert not process(test_engine, transport)
    with Session(test_engine) as session:
        delivery = session.scalars(select(Delivery)).one()
        dead_letter = session.get_one(DeadLetterEvent, delivery.id)
        assert (delivery.status, delivery.attempt_count) == ("dead_lettered", MAX_ATTEMPTS)
        assert dead_letter.reason == "retryable_http_status"
        assert len(session.scalars(select(DeliveryAttempt)).all()) == MAX_ATTEMPTS


def test_workers_skip_locked_deliveries(postgres_client, test_engine):
    add_endpoints(test_engine, True)
    assert upload(postgres_client, EVENT, {**EVENT, "event_id": "relay-2"}).status_code == 200
    first_claimed = ThreadEvent()
    release_first = ThreadEvent()

    def first_receiver(request):
        assert json.loads(request.content)["event_id"] == "relay-1"
        first_claimed.set()
        assert release_first.wait(10)
        return httpx.Response(200)

    def second_receiver(request):
        assert json.loads(request.content)["event_id"] == "relay-2"
        return httpx.Response(200)

    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(process, test_engine, httpx.MockTransport(first_receiver))
        try:
            assert first_claimed.wait(10)
            second = pool.submit(process, test_engine, httpx.MockTransport(second_receiver))
            assert second.result(timeout=5)
        finally:
            release_first.set()
        assert first.result(timeout=5)
    with Session(test_engine) as session:
        assert session.scalars(select(Delivery.status)).all() == ["delivered", "delivered"]
        assert len(session.scalars(select(DeliveryAttempt)).all()) == 2
