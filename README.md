# OpsPilot AI

A webhook relay with reliable delivery and a safe investigation assistant. It accepts signed events, delivers them to customer endpoints with automatic retries, and helps an operator diagnose persistent failures. Any manual retry needs explicit human approval.

**Stack:** FastAPI, PostgreSQL, SQLAlchemy, HTTPX, structlog, pytest, Docker Compose

**Result:** the investigator diagnosed 3 of 3 seeded failure scenarios (HTTP 503, 401, 429) from real delivery data produced by the relay worker. See [Results](#results) for what this does and doesn't show.

## How it works

```mermaid
flowchart LR
    Source[Webhook sender or file upload] --> API[FastAPI ingestion]
    API -->|event + deliveries, one transaction| DB[(PostgreSQL)]
    Worker[Delivery worker] -->|claim and record attempts| DB
    Worker -->|signed request + idempotency key| Customer[Customer endpoint]
    Operator -->|ask a question| Agent[Investigator]
    Agent -->|read-only tools| DB
    Agent -->|keyword search| Docs[Markdown docs]
    Agent -->|propose exact retry| DB
    Operator -->|review and approve| Approval[Approval API]
    Approval -->|consume approval, queue one attempt| DB
```

1. **Ingest:** `POST /webhook` verifies an HMAC signature and deduplicates by event ID. `POST /upload` accepts CSV or JSON and quarantines invalid rows.
2. **Deliver:** each accepted event queues a delivery for every enabled endpoint, in the same transaction as the event. A worker sends signed requests and records every attempt.
3. **Recover:** transient failures (timeouts, 408, 429, 5xx) retry with exponential backoff and jitter, up to five attempts. Permanent failures and exhausted retries go to a dead-letter table.
4. **Investigate:** an operator asks why an endpoint is failing. The investigator calls read-only tools, cites the evidence and documentation it used, and proposes a retry when one is appropriate.
5. **Approve:** a human reviews the exact retry and approves it. The backend executes it and verifies the result.

## Key design decisions

- **Atomic queueing.** An event and its deliveries commit together or not at all. Duplicates and quarantined rows create no deliveries.
- **At-least-once delivery.** Every request carries a stable `Idempotency-Key`. If a worker crashes after the receiver accepts a request, the next attempt resends it and the receiver deduplicates.
- **Safe concurrency.** Workers claim rows with `FOR UPDATE SKIP LOCKED` and a 30-second lease. Expired leases are recovered, and the interrupted attempt is recorded and counted toward the retry limit.
- **The agent cannot act.** It can read delivery data and request an approval. It cannot approve, queue, execute or verify a retry. Only the backend can.
- **Hash-bound approvals.** An approval stores the exact action and a SHA-256 of its arguments. It is single-use, expires after 15 minutes, and is rejected if the delivery's state has changed.
- **Verified outcomes.** The backend reads the recorded delivery result after an approved retry. Success is never taken from the agent's word.

## Quickstart

Requires Docker Desktop. Commands are PowerShell, as tested on Windows.

```powershell
$env:OPSPILOT_OPERATOR_TOKEN = [guid]::NewGuid().ToString('N')
docker compose up --build --wait
$scenarios = docker compose exec -T worker python -m app.seed_scenarios | ConvertFrom-Json
$scenarios | Format-Table
```

Wait for the first delivery attempts, then ask the investigator about a failing endpoint:

```powershell
$headers = @{ Authorization = "Bearer $env:OPSPILOT_OPERATOR_TOKEN" }
Invoke-RestMethod 'http://127.0.0.1:8000/operator/investigations' -Method Post -Headers $headers `
  -ContentType 'application/json' -Body (@{
    endpoint_id = $scenarios[0].endpoint_id
    delivery_id = $scenarios[0].delivery_id
    question   = 'Why is this customer endpoint failing?'
  } | ConvertTo-Json) | ConvertTo-Json -Depth 8
```

Interactive API docs: `http://127.0.0.1:8000/docs`. The mock customer's controls: `http://127.0.0.1:8001/docs`.

The full walkthrough, including the approval flow, is in [docs/demo.md](docs/demo.md).

## Results

The investigator matched the expected diagnosis in 3 of 3 seeded scenarios. Expected answers are stored outside the investigator's inputs.

| Observed failure | Diagnosis | First citation |
| --- | --- | --- |
| HTTP 503 | Customer endpoint unavailable | `docs/retries.md` |
| HTTP 401 | Authentication or signing secret mismatch | `docs/authentication.md` |
| HTTP 429 | Customer endpoint rate-limiting requests | `docs/rate-limits.md` |

**What this shows:** the relay produces real attempt data, the investigator reads it through tools, and approval gating works end to end.
**What it doesn't show:** performance on diverse failures. The default investigator is rule-based with keyword retrieval, so this is a baseline over three fixtures.

## Testing

63 automated tests cover signature verification, idempotency, upload validation and quarantine, transaction rollback, relay queueing, retry classification, crash recovery, and approval hashing, expiry and reuse. They include an end-to-end test with real loopback HTTP servers and a separate worker process.

```powershell
$env:OPSPILOT_TEST_DATABASE_URL = 'postgresql+psycopg://USER:PASSWORD@127.0.0.1:PORT/opspilot_test'
.\.venv\Scripts\python.exe -m pytest -q
```

Integration tests need an isolated PostgreSQL database named `opspilot_test` and refuse to run against the development database. Details are in [docs/testing.md](docs/testing.md).

## Known limitations

- **No tenant isolation.** All enabled endpoints share one routing list.
- **No user authentication.** Operator routes use a single shared demo token, with no accounts, roles or per-person approval identity.
- **Small evaluation.** Results cover three seeded scenarios. The default investigator uses rules and keyword search. An optional OpenAI-written explanation can be enabled, but it does not influence diagnosis, approval or execution.
- **No vector search.** Documentation retrieval is keyword-based. pgvector is not used.
- **At-least-once delivery.** Receivers must deduplicate by idempotency key.

## Documentation

- [docs/demo.md](docs/demo.md): failure scenarios, local setup, approval walkthrough
- [docs/api.md](docs/api.md): endpoints and request/response details
- [docs/testing.md](docs/testing.md): test setup and reproduction commands
