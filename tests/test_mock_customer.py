import hashlib
import hmac

import pytest
from fastapi.testclient import TestClient

from app.relay import HTTP_TIMEOUT_SECONDS, endpoint_url
from mock_customer import main

SECRET = "unit-test-customer-secret"
BODY = b'{"event_id":"demo","event_type":"demo.created","payload":{"amount":1}}'


def signed_headers(body=BODY):
    digest = hmac.new(SECRET.encode(), body, hashlib.sha256).hexdigest()
    return {"X-Signature": f"sha256={digest}", "Idempotency-Key": "demo-key"}


@pytest.fixture
def customer():
    with TestClient(main.create_app(SECRET)) as client:
        yield client


def test_healthy_delivery_is_authenticated_and_deduplicated(customer):
    assert customer.get("/health").json() == {"status": "healthy", "signing_configured": True}
    headers = signed_headers()
    assert customer.post("/webhook", content=BODY, headers=headers).json() == {"status": "processed"}
    assert customer.post("/webhook", content=BODY, headers=headers).json() == {"status": "duplicate"}
    changed = BODY + b" "
    assert customer.post("/webhook", content=changed, headers=signed_headers(changed)).status_code == 409
    assert customer.get("/mode").json() == {"mode": "healthy", "processed": 1, "duplicates": 1}


def test_rejects_invalid_signatures_and_missing_key(customer):
    headers = signed_headers()
    assert customer.post("/webhook", content=BODY).status_code == 401
    assert customer.post("/webhook", content=BODY + b" ", headers=headers).status_code == 401
    assert customer.post("/webhook", content=BODY, headers={"X-Signature": "sha256=bad"}).status_code == 401
    del headers["Idempotency-Key"]
    assert customer.post("/webhook", content=BODY, headers=headers).status_code == 400
    assert customer.post("/webhook", content=b"{}", headers=signed_headers(b"{}")).status_code == 422
    assert customer.post("/mode", json={"mode": "bad_mode"}).status_code == 422
    assert customer.get("/mode").json()["processed"] == 0


def test_unconfigured_signing_fails_closed():
    with TestClient(main.create_app("")) as customer:
        assert customer.get("/health").json()["signing_configured"] is False
        assert customer.post("/webhook", content=BODY, headers=signed_headers()).status_code == 503


@pytest.mark.parametrize(("mode", "status"), [
    ("server_error", 500), ("unavailable", 503), ("unauthorized", 401), ("rate_limited", 429),
])
def test_switch_failure_modes_and_recover(customer, mode, status):
    assert customer.post("/mode", json={"mode": mode}).json() == {"mode": mode}
    assert customer.get("/mode").json()["mode"] == mode
    failed = customer.post("/webhook", content=BODY, headers=signed_headers())
    assert failed.status_code == status
    if status == 429:
        assert failed.headers["Retry-After"] == "10"
    assert customer.get("/mode").json()["processed"] == 0
    assert customer.post("/mode", json={"mode": "healthy"}).status_code == 200
    assert customer.post("/webhook", content=BODY, headers=signed_headers()).json() == {"status": "processed"}


def test_timeout_waits_longer_than_worker_timeout(customer, monkeypatch):
    delays = []

    async def capture_sleep(delay):
        delays.append(delay)

    monkeypatch.setattr(main, "asyncio", type("Clock", (), {"sleep": staticmethod(capture_sleep)}))
    assert customer.post("/mode", json={"mode": "timeout"}).status_code == 200
    assert customer.post("/webhook", content=BODY, headers=signed_headers()).status_code == 503
    assert len(delays) == 1 and delays[0] > HTTP_TIMEOUT_SECONDS
    assert customer.get("/mode").json()["processed"] == 0


@pytest.mark.parametrize("url", ["http://127.0.0.1:8001/webhook", "http://[::1]:8001/webhook"])
def test_loopback_http_requires_explicit_opt_in(url):
    with pytest.raises(ValueError):
        endpoint_url(url)
    assert str(endpoint_url(url, allow_loopback_http=True)) == url


@pytest.mark.parametrize("url", [
    "http://localhost:8001/webhook", "http://127.0.0.1.example.com/webhook",
    "http://192.168.1.1/webhook", "http://user:pass@127.0.0.1/webhook",
    "http://127.0.0.1/webhook#fragment", "https://user:pass@example.com/webhook",
])
def test_opt_in_preserves_destination_and_url_restrictions(url):
    with pytest.raises(ValueError):
        endpoint_url(url, allow_loopback_http=True)


def test_https_remains_available_by_default():
    assert endpoint_url("https://example.com/webhook").scheme == "https"
