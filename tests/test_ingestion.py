import csv
import hashlib
import hmac
import io
import json
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from uuid import UUID

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app import main, security
from app.ingestion import process_records
from app.models import Event, QuarantinedRecord


def event(event_id="event-1"):
    return {"event_id": event_id, "event_type": "order.created", "payload": {"amount": 5}}


def sign(body, secret):
    return "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


@pytest.mark.parametrize("signature", [None, "", "sha256=abc", "sha256=" + "é" * 64,
                                       "SHA256=" + "a" * 64, "sha256=" + "0" * 64])
def test_rejects_missing_malformed_or_wrong_signature(client, signature):
    headers = [] if signature is None else [(b"X-Signature", signature.encode("latin-1"))]
    response = client.post("/webhook", content=json.dumps(event()), headers=headers)
    assert response.status_code == 401
    assert UUID(response.headers["X-Correlation-ID"]).version == 4


def test_valid_signature_and_tampered_payload(client, signing_secret, monkeypatch):
    body = json.dumps(event()).encode()
    headers = {"X-Signature": sign(body, signing_secret)}
    monkeypatch.setattr(main, "persist_webhook", lambda value: True)
    assert client.post("/webhook", content=body, headers=headers).json()["status"] == "processed"
    assert client.post("/webhook", content=body + b" ", headers=headers).status_code == 401


def test_missing_signing_secret_fails_closed(signing_secret, monkeypatch):
    body = b"event"
    monkeypatch.setattr(security, "WEBHOOK_SECRET", None)
    assert not security.verify_signature(body, sign(body, signing_secret))


@pytest.mark.parametrize("body", [b"{", b"[]", b'{"event_id": "x", "payload": []}'])
def test_signed_invalid_webhook_payload(client, signing_secret, body):
    response = client.post("/webhook", content=body, headers={"X-Signature": sign(body, signing_secret)})
    assert response.status_code == 422
    assert response.json() == {"detail": "Invalid event payload"}


@pytest.mark.parametrize(
    ("filename", "content"),
    [("data.json", b"{"), ("data.json", b"{}"), ("data.csv", b"\xff"),
     ("data.csv", b'"missing end quote'), ("data.json", b""), ("data.txt", b"abc")],
)
def test_invalid_upload_envelope(client, filename, content):
    response = client.post("/upload", files={"file": (filename, content)})
    assert response.status_code == 400
    assert response.headers["X-Correlation-ID"]


def test_safe_json_logs_and_request_correlation(client, signing_secret, monkeypatch, capsys):
    body = json.dumps({**event(), "payload": {"private": "payload-sentinel"}}).encode()
    signature = sign(body, signing_secret)

    def fail(event):
        raise RuntimeError(f"{signing_secret} {signature} {body!r}")

    monkeypatch.setattr(main, "persist_webhook", fail)
    failed = client.post("/webhook", content=body, headers={"X-Signature": signature})
    healthy = client.get("/health", headers={"X-Correlation-ID": "untrusted-client-value"})
    assert failed.status_code == 500
    assert healthy.status_code == 200
    ids = [response.headers["X-Correlation-ID"] for response in (failed, healthy)]
    assert all(UUID(value).version == 4 for value in ids)
    assert ids[0] != ids[1]

    output = capsys.readouterr().out
    logs = [json.loads(line) for line in output.splitlines()]
    assert [entry["event"] for entry in logs] == [
        "webhook_persistence_failed", "request_completed", "request_completed",
    ]
    assert [entry["correlation_id"] for entry in logs] == [ids[0], ids[0], ids[1]]
    assert all(secret not in output for secret in
               (signing_secret, signature, "payload-sentinel", "untrusted-client-value"))


def test_webhook_persistence_and_idempotency(postgres_client, test_engine, signing_secret):
    body = json.dumps(event()).encode()
    headers = {"X-Signature": sign(body, signing_secret)}
    first = postgres_client.post("/webhook", content=body, headers=headers)
    second = postgres_client.post("/webhook", content=body, headers=headers)
    assert first.status_code == second.status_code == 200
    assert first.json() == {"event_id": "event-1", "status": "processed"}
    assert second.json() == {"event_id": "event-1", "status": "already_processed"}
    with Session(test_engine) as session:
        stored = session.scalars(select(Event)).one()
        assert stored.event_id == "event-1"
        assert stored.event_type == "order.created"
        assert stored.payload == {"amount": 5}
        assert session.scalars(select(QuarantinedRecord)).first() is None


def test_concurrent_duplicate_webhooks(postgres_client, test_engine, signing_secret):
    body = json.dumps(event()).encode()
    headers = {"X-Signature": sign(body, signing_secret)}
    # Wait inside each request so the test exercises overlapping inserts.
    insert = main.insert_event
    insert_barrier = Barrier(2, timeout=10)

    def simultaneous_insert(session, value):
        insert_barrier.wait()
        return insert(session, value)

    with pytest.MonkeyPatch.context() as patch, ThreadPoolExecutor(max_workers=2) as pool:
        patch.setattr(main, "insert_event", simultaneous_insert)
        requests = [
            pool.submit(postgres_client.post, "/webhook", content=body, headers=headers)
            for _ in range(2)
        ]
        responses = [request.result(timeout=20) for request in requests]
    assert all(response.status_code == 200 for response in responses)
    assert sorted(response.json()["status"] for response in responses) == [
        "already_processed", "processed",
    ]
    with Session(test_engine) as session:
        assert session.scalars(select(Event.event_id)).all() == ["event-1"]


def test_csv_quarantines_bad_rows_and_keeps_following_records(postgres_client, test_engine):
    content = io.StringIO(newline="")
    writer = csv.writer(content)
    writer.writerow(["event_id", "event_type", "payload"])
    writer.writerow(["first", "order.created", '{"amount":\n5}'])
    writer.writerow(["bad", "order.created", "{broken"])
    writer.writerow(["short", "order.created"])
    content.write('malformed,order.created,"bad"x\r\n')
    writer.writerow(["last", "order.created", '{"amount":6}'])

    response = postgres_client.post("/upload", files={"file": ("events.csv", content.getvalue())})
    assert response.status_code == 200
    assert response.json() == {"filename": "events.csv", "accepted": 2, "rejected": 3}
    with Session(test_engine) as session:
        assert session.scalars(select(Event.event_id).order_by(Event.id)).all() == ["first", "last"]
        bad_json, short, malformed = session.scalars(
            select(QuarantinedRecord).order_by(QuarantinedRecord.row_number)
        ).all()
        assert [row.row_number for row in (bad_json, short, malformed)] == [2, 3, 4]
        assert json.loads(bad_json.raw_data)["payload"] == "{broken"
        assert short.error_reason == "Incorrect CSV column count"
        assert malformed.error_reason == "Invalid CSV row"
        assert bad_json.created_at.utcoffset() is not None


def test_json_quarantines_validation_errors_and_duplicates(postgres_client, test_engine):
    records = [
        event(), event(), {**event("bad"), "payload": []}, "not a record",
        {**event("nonfinite"), "payload": {"value": float("nan")}},
        event("nul\x00"), event("last"),
    ]
    files = {"file": ("events.json", json.dumps(records))}
    first = postgres_client.post("/upload", files=files)
    second = postgres_client.post("/upload", files=files)
    assert first.json() == {"filename": "events.json", "accepted": 2, "rejected": 5}
    assert second.json() == {"filename": "events.json", "accepted": 0, "rejected": 7}
    with Session(test_engine) as session:
        assert session.scalars(select(Event.event_id).order_by(Event.id)).all() == ["event-1", "last"]
        quarantined = session.scalars(select(QuarantinedRecord).order_by(QuarantinedRecord.id)).all()
        assert len(quarantined) == 12
        assert quarantined[0].error_reason == "Duplicate event_id"
        assert quarantined[0].row_number == 2
        assert json.loads(quarantined[0].raw_data) == event()


def test_database_failure_rolls_back_whole_batch(postgres_client, test_engine):
    with test_engine.begin() as connection:
        connection.exec_driver_sql(
            "ALTER TABLE quarantined_records ADD CHECK (row_number <> 3)"
        )
    records = [event(), {}, {}]
    response = postgres_client.post("/upload", files={"file": ("events.json", json.dumps(records))})
    assert response.status_code == 500
    assert response.json() == {"detail": "File ingestion failed"}

    with Session(test_engine) as session:
        with pytest.raises(IntegrityError):
            process_records(records, session)
        assert session.is_active
        assert not session.in_transaction()
        assert session.scalars(select(Event)).all() == []
        assert session.scalars(select(QuarantinedRecord)).all() == []
        assert process_records([event()], session) == {"accepted": 1, "rejected": 0}
