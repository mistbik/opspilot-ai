import json
import os
import socket
import subprocess
import sys
from contextlib import contextmanager
from threading import Thread
from time import monotonic, sleep

import httpx
import uvicorn
from sqlalchemy import select, text
from sqlalchemy.orm import Session

from app import database, main, worker
from app.models import DeadLetterEvent, Delivery, DeliveryAttempt, Event
from app.security import sign_payload
from mock_customer.main import create_app

OUTBOUND_SECRET = "loopback-test-signing-secret"


def wait_for(check, timeout=15):
    deadline = monotonic() + timeout
    while monotonic() < deadline:
        result = check()
        if result:
            return result
        sleep(0.1)
    raise AssertionError("Timed out waiting for local service or delivery state")


@contextmanager
def serve(app):
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.bind(("127.0.0.1", 0))
        host, port = listener.getsockname()
        server = uvicorn.Server(uvicorn.Config(
            app, log_config=None, access_log=False, lifespan="on", timeout_graceful_shutdown=2
        ))
        thread = Thread(target=server.run, kwargs={"sockets": [listener]}, daemon=True)
        thread.start()
        try:
            wait_for(lambda: server.started)
            yield f"http://{host}:{port}"
        finally:
            server.should_exit = True
            thread.join(timeout=5)
            assert not thread.is_alive()


@contextmanager
def worker_process(engine):
    url = engine.url
    with engine.connect() as connection:
        schema = connection.scalar(text("SELECT current_schema()"))
    environment = os.environ | {
        "POSTGRES_HOST": url.host or "",
        "POSTGRES_PORT": str(url.port or 5432),
        "POSTGRES_DB": url.database,
        "POSTGRES_USER": url.username or "",
        "POSTGRES_PASSWORD": url.password or "",
        "PGOPTIONS": f"-csearch_path={schema}",
    }
    with subprocess.Popen(
        [sys.executable, "-m", "app.worker", "run", "--allow-loopback-http"],
        env=environment,
    ) as process:
        try:
            yield process
        finally:
            if process.poll() is None:
                process.terminate()
                process.wait(timeout=5)


def delivery_state(engine, event_id):
    with Session(engine) as session:
        delivery = session.scalars(
            select(Delivery).join(Event).where(Event.event_id == event_id)
        ).one()
        attempts = session.scalars(
            select(DeliveryAttempt)
            .where(DeliveryAttempt.delivery_id == delivery.id)
            .order_by(DeliveryAttempt.attempt_number)
        ).all()
        dead_letter = session.get(DeadLetterEvent, delivery.id)
        return (
            delivery.status,
            [(attempt.outcome, attempt.status_code) for attempt in attempts],
            dead_letter.reason if dead_letter else None,
            str(delivery.idempotency_key),
        )


def test_local_relay_over_real_http(test_engine, signing_secret, monkeypatch):
    monkeypatch.setattr(database, "engine", test_engine)
    monkeypatch.setattr(main, "engine", test_engine)
    monkeypatch.setattr(worker, "engine", test_engine)
    assert worker.check_database() == 1

    with (serve(main.app) as api, serve(create_app(OUTBOUND_SECRET)) as customer,
          httpx.Client(timeout=3, trust_env=False) as http):
        assert worker.check_database() == 0
        answers = iter([f"{customer}/webhook", OUTBOUND_SECRET])
        monkeypatch.setattr(worker, "getpass", lambda prompt: next(answers))
        monkeypatch.setattr(sys, "argv", ["worker", "add-endpoint", "--allow-loopback-http"])
        assert worker.main() == 0

        def set_mode(mode):
            response = http.post(f"{customer}/mode", json={"mode": mode})
            assert response.json() == {"mode": mode}

        def submit(event_id):
            event = {"event_id": event_id, "event_type": "demo.created", "payload": {"amount": 5}}
            body = json.dumps(event, separators=(",", ":")).encode()
            response = http.post(
                f"{api}/webhook", content=body,
                headers={"X-Signature": sign_payload(body, signing_secret)},
            )
            assert response.json() == {"status": "processed", "event_id": event_id}
            return body

        with worker_process(test_engine) as relay_process:
            def wait_state(event_id, status, attempt_count):
                def check():
                    assert relay_process.poll() is None, "Relay worker exited prematurely"
                    state = delivery_state(test_engine, event_id)
                    if state[0] == status and len(state[1]) == attempt_count:
                        if state[1][-1][0] != "in_progress":
                            return state
                    return None

                return wait_for(check)

            body = submit("healthy")
            _, attempts, _, key = wait_state("healthy", "delivered", 1)
            assert attempts == [("delivered", 200)]
            replay = http.post(f"{customer}/webhook", content=body, headers={
                "X-Signature": sign_payload(body, OUTBOUND_SECRET), "Idempotency-Key": key,
            })
            assert replay.json() == {"status": "duplicate"}

            set_mode("unavailable")
            submit("transient")
            _, attempts, _, first_key = wait_state("transient", "pending", 1)
            assert attempts == [("retryable_http_status", 503)]
            set_mode("healthy")
            _, attempts, _, retry_key = wait_state("transient", "delivered", 2)
            assert attempts == [("retryable_http_status", 503), ("delivered", 200)]
            assert retry_key == first_key

            set_mode("timeout")
            submit("slow")
            _, attempts, _, _ = wait_state("slow", "pending", 1)
            assert attempts == [("timeout", None)]
            set_mode("healthy")
            _, attempts, _, _ = wait_state("slow", "delivered", 2)
            assert attempts == [("timeout", None), ("delivered", 200)]

            set_mode("unauthorized")
            submit("permanent")
            _, attempts, reason, _ = wait_state("permanent", "dead_lettered", 1)
            assert attempts == [("permanent_http_status", 401)]
            assert reason == "permanent_http_status"
            assert http.get(f"{customer}/mode").json() == {
                "mode": "unauthorized", "processed": 3, "duplicates": 1,
            }
