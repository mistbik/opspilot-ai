import hashlib
import json
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from uuid import UUID

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from app import investigation_summary
from app.approvals import arguments_hash
from app.investigation_tools import ServiceTools
from app.models import DeadLetterEvent, Delivery, DeliveryAttempt, Endpoint, Event, RetryApproval
from app.relay import MAX_ATTEMPTS, process_next_delivery
from app.seed_scenarios import seed_scenarios
from mock_customer.main import create_app

OPERATOR = {"Authorization": "Bearer pytest-operator-token"}
INVESTIGATIONS = "/operator/investigations"
APPROVALS = "/operator/retry-approvals"


@pytest.fixture(autouse=True)
def operator_settings(monkeypatch):
    monkeypatch.setenv("OPSPILOT_OPERATOR_TOKEN", "pytest-operator-token")
    monkeypatch.delenv("OPSPILOT_OPENAI_API_KEY", raising=False)


@pytest.fixture
def tool_calls(monkeypatch):
    calls = []

    def trace(name, method):
        def call(*args, **kwargs):
            calls.append(name)
            return method(*args, **kwargs)
        return call

    for name in (
        "get_endpoint_health", "get_delivery_history", "get_delivery_failure",
        "search_documentation", "request_retry_approval",
    ):
        monkeypatch.setattr(ServiceTools, name, trace(name, getattr(ServiceTools, name)))
    return calls


def run_attempt(engine, receiver, *, loopback=False):
    with Session(engine) as session, httpx.Client(transport=httpx.MockTransport(receiver)) as client:
        return process_next_delivery(session, client, allow_loopback_http=loopback)


@pytest.mark.parametrize(("status", "document"), [
    (503, "retries.md"), (401, "authentication.md"), (429, "rate-limits.md"),
])
def test_seed_scenarios_and_investigate_observed_failures(
    postgres_client, test_engine, status, document, tool_calls
):
    secret = "mock-secret"
    with Session(test_engine) as session:
        scenarios = seed_scenarios(session, secret)
        assert seed_scenarios(session, secret) == scenarios

    with TestClient(create_app(secret)) as mock:
        def receiver(request):
            result = mock.post(request.url.path, content=request.content, headers=request.headers)
            return httpx.Response(result.status_code, headers=result.headers)

        assert all(run_attempt(test_engine, receiver, loopback=True) for _ in scenarios)

    chosen = next(scenario for scenario in scenarios if scenario["expected_status"] == status)
    response = postgres_client.post(INVESTIGATIONS, headers=OPERATOR, json={
        "endpoint_id": chosen["endpoint_id"], "delivery_id": chosen["delivery_id"],
        "question": "Why is this customer endpoint failing?",
    })
    assert response.status_code == 200
    report = response.json()
    assert report["likely_root_cause"] == chosen["expected_root_cause"]
    assert report["evidence"]["http_status"] == status
    assert report["citations"][0]["filename"] == document
    assert report["approval"] is None
    assert secret not in response.text
    assert report["model_summary"] is None
    assert tool_calls == [
        "get_endpoint_health", "get_delivery_history", "get_delivery_failure", "search_documentation",
    ]
    assert postgres_client.post(INVESTIGATIONS, headers=OPERATOR, json={
        "endpoint_id": chosen["endpoint_id"],
        "delivery_id": next(row["delivery_id"] for row in scenarios if row != chosen),
        "question": "Inspect a delivery belonging to another endpoint.",
    }).status_code == 404

    payload = [{"event_id": "normal-event", "event_type": "normal", "payload": {}}]
    postgres_client.post("/upload", files={"file": ("events.json", json.dumps(payload))})
    with Session(test_engine) as session:
        assert session.scalar(select(func.count()).select_from(Delivery)) == 3
        assert session.scalar(select(func.count()).select_from(Event)) == 4


@pytest.fixture
def dead_letter(postgres_client, test_engine):
    with Session(test_engine) as session, session.begin():
        endpoint = Endpoint(url="https://customer.example/webhook", signing_secret="fake-secret")
        session.add(endpoint)
        session.flush()
        endpoint_id = endpoint.id
    response = postgres_client.post("/upload", files={"file": (
        "events.json", '[{"event_id":"approval-test","event_type":"demo","payload":{}}]',
    )})
    assert response.json()["accepted"] == 1
    for _ in range(MAX_ATTEMPTS):
        with Session(test_engine) as session, session.begin():
            session.execute(update(Delivery).values(next_attempt_at=func.now()))
        assert run_attempt(test_engine, lambda request: httpx.Response(503))
    return endpoint_id


def new_approval(client, endpoint_id):
    response = client.post(INVESTIGATIONS, headers=OPERATOR, json={
        "endpoint_id": endpoint_id, "question": "Investigate and recommend next steps.",
    })
    assert response.status_code == 200
    approval = response.json()["approval"]
    assert approval["status"] == "awaiting_approval"
    return approval


def approval_body(approval):
    return {"action": approval["action"], "arguments": approval["arguments"]}


@pytest.mark.parametrize("retry_code", [200, 503])
def test_exact_human_approval_is_required_and_result_is_verified(
    postgres_client, test_engine, dead_letter, retry_code, tool_calls
):
    approval = new_approval(postgres_client, dead_letter)
    assert tool_calls == [
        "get_endpoint_health", "get_delivery_history", "get_delivery_failure",
        "search_documentation", "request_retry_approval",
    ]
    assert new_approval(postgres_client, dead_letter)["approval_id"] == approval["approval_id"]
    route = f"{APPROVALS}/{approval['approval_id']}"
    action = approval_body(approval)
    assert not run_attempt(test_engine, lambda request: pytest.fail("Unapproved retry"))
    assert postgres_client.post(f"{route}/approve", json=action).status_code == 401
    assert postgres_client.post(f"{route}/approve", headers=OPERATOR, json={
        **action, "arguments": {**action["arguments"], "delivery_id": 999},
    }).status_code == 409
    assert postgres_client.post(f"{route}/approve", headers=OPERATOR, json={
        **action, "arguments": {**action["arguments"], "expected_attempt_count": 1},
    }).status_code == 409
    assert postgres_client.post(f"{route}/approve", headers=OPERATOR, json={
        **action, "action": "another_action",
    }).status_code == 422
    accepted = postgres_client.post(f"{route}/approve", headers=OPERATOR, json=action)
    assert accepted.status_code == 202 and accepted.json()["status"] == "queued"
    assert postgres_client.post(f"{route}/approve", headers=OPERATOR, json=action).status_code == 409

    with Session(test_engine) as session:
        key = str(session.scalar(select(Delivery.idempotency_key)))

    def receiver(request):
        assert request.headers["Idempotency-Key"] == key
        return httpx.Response(retry_code)

    assert run_attempt(test_engine, receiver)
    assert not run_attempt(test_engine, lambda request: pytest.fail("Approval authorized multiple sends"))
    checked = postgres_client.get(route, headers=OPERATOR).json()
    assert checked["status"] == "completed"
    assert checked["result"]["http_status"] == retry_code
    assert checked["result"]["delivered"] == (retry_code == 200)
    assert checked["result"]["attempt_number"] == MAX_ATTEMPTS + 1


def test_arguments_hash_is_canonical_and_binds_values():
    expected = hashlib.sha256(b'{"delivery_id":17,"expected_attempt_count":5}').hexdigest()
    assert arguments_hash({"delivery_id": 17, "expected_attempt_count": 5}) == expected
    assert arguments_hash({"expected_attempt_count": 5, "delivery_id": 17}) == expected
    assert arguments_hash({"delivery_id": 18, "expected_attempt_count": 5}) != expected
    assert arguments_hash({"delivery_id": 17, "expected_attempt_count": 6}) != expected


def test_approval_rejects_a_mismatched_stored_hash(postgres_client, test_engine, dead_letter):
    approval = new_approval(postgres_client, dead_letter)
    approval_id = UUID(approval["approval_id"])
    with Session(test_engine) as session, session.begin():
        session.get_one(RetryApproval, approval_id).arguments_hash = "0" * 64
    response = postgres_client.post(
        f"{APPROVALS}/{approval_id}/approve", headers=OPERATOR, json=approval_body(approval)
    )
    assert response.status_code == 409
    assert not run_attempt(test_engine, lambda request: pytest.fail("Retry with mismatched hash"))


def test_expired_approval_is_rejected(postgres_client, test_engine, dead_letter):
    approval = new_approval(postgres_client, dead_letter)
    approval_id = UUID(approval["approval_id"])
    with Session(test_engine) as session, session.begin():
        session.get_one(RetryApproval, approval_id).expires_at = func.now()
    route = f"{APPROVALS}/{approval_id}"
    assert postgres_client.get(route, headers=OPERATOR).json()["status"] == "expired"
    assert postgres_client.post(f"{route}/approve", headers=OPERATOR, json=approval_body(approval)).status_code == 410
    assert not run_attempt(test_engine, lambda request: pytest.fail("Expired retry"))


def test_concurrent_approval_is_consumed_once(postgres_client, test_engine, dead_letter):
    approval = new_approval(postgres_client, dead_letter)
    barrier = Barrier(2, timeout=5)

    def consume():
        barrier.wait()
        return postgres_client.post(
            f"{APPROVALS}/{approval['approval_id']}/approve", headers=OPERATOR,
            json=approval_body(approval),
        ).status_code

    with ThreadPoolExecutor(max_workers=2) as pool:
        requests = [pool.submit(consume) for _ in range(2)]
        assert sorted(request.result(timeout=10) for request in requests) == [202, 409]


def test_operator_routes_fail_closed_without_token(client, monkeypatch):
    monkeypatch.delenv("OPSPILOT_OPERATOR_TOKEN")
    assert client.post(INVESTIGATIONS, json={"endpoint_id": 1, "question": "Why?"}).status_code == 503


def test_model_summary_sends_only_bounded_evidence(monkeypatch):
    monkeypatch.setenv("OPSPILOT_OPENAI_API_KEY", "test-key")
    monkeypatch.setenv("OPSPILOT_INVESTIGATION_MODEL", "test-model")
    original_client = httpx.Client

    def respond(request):
        payload = json.loads(request.content)
        assert payload["model"] == "test-model"
        assert payload["store"] is False
        assert "secret-data" not in payload["input"]
        assert json.loads(payload["input"])["http_status"] == 503
        return httpx.Response(200, json={
            "status": "completed",
            "output": [{"type": "message", "content": [{"type": "output_text", "text": "Check the receiver."}]}],
        })

    monkeypatch.setattr(investigation_summary.httpx, "Client", lambda **kwargs: original_client(
        transport=httpx.MockTransport(respond), **kwargs
    ))
    report = {
        "evidence": {"http_status": 503, "outcome": "retryable_http_status", "delivery_status": "pending"},
        "likely_root_cause": "Unavailable", "recommendation": "Check it", "citations": [],
        "approval": "secret-data", "question": "secret-data",
    }
    assert investigation_summary.model_summary(report) == "Check the receiver."
