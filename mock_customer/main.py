import asyncio
import hashlib
import os
from enum import StrEnum
from typing import Annotated

from fastapi import FastAPI, Header, HTTPException, Request
from pydantic import BaseModel, ValidationError

from app.relay import HTTP_TIMEOUT_SECONDS
from app.schemas import WebhookEvent
from app.security import signature_matches

TIMEOUT_DELAY_SECONDS = HTTP_TIMEOUT_SECONDS + 2


class Mode(StrEnum):
    HEALTHY = "healthy"
    SERVER_ERROR = "server_error"
    UNAVAILABLE = "unavailable"
    UNAUTHORIZED = "unauthorized"
    RATE_LIMITED = "rate_limited"
    TIMEOUT = "timeout"


class ModeSetting(BaseModel):
    mode: Mode


FAILURES = {
    Mode.SERVER_ERROR: 500,
    Mode.UNAVAILABLE: 503,
    Mode.UNAUTHORIZED: 401,
    Mode.RATE_LIMITED: 429,
}
SCENARIO_MODES = {
    "endpoint-down": Mode.UNAVAILABLE,
    "expired-signing-secret": Mode.HEALTHY,
    "rate-limiting": Mode.RATE_LIMITED,
}


def create_app(signing_secret: str | None = None) -> FastAPI:
    app = FastAPI(title="OpsPilot Mock Customer")
    secret = signing_secret if signing_secret is not None else os.getenv("MOCK_CUSTOMER_SIGNING_SECRET")
    mode = Mode.HEALTHY
    scenario_modes = SCENARIO_MODES.copy()
    accepted_keys: dict[str, bytes] = {}
    duplicates = 0

    @app.get("/health")
    async def health():
        return {"status": "healthy", "signing_configured": bool(secret)}

    @app.get("/mode")
    async def get_mode():
        return {"mode": mode, "processed": len(accepted_keys), "duplicates": duplicates}

    @app.post("/mode", response_model=ModeSetting)
    async def set_mode(setting: ModeSetting):
        nonlocal mode
        mode = setting.mode
        return setting

    @app.post("/scenarios/{scenario}/mode", response_model=ModeSetting)
    async def set_scenario_mode(scenario: str, setting: ModeSetting):
        if scenario not in scenario_modes:
            raise HTTPException(404, "Unknown scenario")
        scenario_modes[scenario] = setting.mode
        return setting

    @app.post("/webhook")
    @app.post("/scenarios/{scenario}/webhook", include_in_schema=False)
    async def webhook(
        request: Request,
        x_signature: Annotated[str | None, Header()] = None,
        idempotency_key: Annotated[str | None, Header()] = None,
    ):
        nonlocal duplicates
        if not secret:
            raise HTTPException(503, "Mock signing secret is not configured")
        body = await request.body()
        if not signature_matches(body, x_signature, secret):
            raise HTTPException(401, "Invalid webhook signature")
        if not idempotency_key or not idempotency_key.strip() or len(idempotency_key) > 255:
            raise HTTPException(400, "A valid Idempotency-Key is required")
        try:
            WebhookEvent.model_validate_json(body)
        except ValidationError:
            raise HTTPException(422, "Invalid event payload") from None

        scenario = request.path_params.get("scenario")
        active_mode = scenario_modes.get(scenario) if scenario else mode
        if active_mode is None:
            raise HTTPException(404, "Unknown scenario")
        if active_mode == Mode.TIMEOUT:
            await asyncio.sleep(TIMEOUT_DELAY_SECONDS)
            raise HTTPException(503, "Simulated slow response")
        if active_mode in FAILURES:
            headers = {"Retry-After": "10"} if active_mode == Mode.RATE_LIMITED else None
            raise HTTPException(FAILURES[active_mode], "Simulated customer failure", headers=headers)

        # No await between lookup and insert, so requests in this process cannot race here.
        digest = hashlib.sha256(body).digest()
        previous = accepted_keys.get(idempotency_key)
        if previous is not None:
            if previous != digest:
                raise HTTPException(409, "Idempotency-Key already used for a different body")
            duplicates += 1
            return {"status": "duplicate"}
        accepted_keys[idempotency_key] = digest
        return {"status": "processed"}

    return app


app = create_app()
