# OpsPilot AI ingestion service

FastAPI accepts signed webhooks and CSV or JSON uploads and stores validated events in PostgreSQL.

OpsPilot queues outbound webhooks, records delivery attempts and dead letters, and helps an operator investigate failures and approve a specific manual retry.

## Architecture

```mermaid
flowchart LR
    Source[Webhook sender or file uploader] --> API[FastAPI ingestion]
    API -->|Commit event and deliveries together| DB[(PostgreSQL)]
    Worker[Delivery worker] -->|Claim deliveries and record outcomes| DB
    Worker -->|Signed request with idempotency key| Receiver[Customer endpoint or local mock]
    Operator[Human operator] -->|Ask a question| Agent[Investigation agent]
    Agent -->|Read through service tools| DB
    Agent -->|Keyword search| Docs[Markdown documentation]
    Agent -->|Propose exact retry arguments| DB
    Operator -->|Review and approve| Approval[Approval API]
    Approval -->|Consume approval and queue one attempt| DB
    DB -->|Recorded attempt outcome| Approval
```

## API reference

Interactive request and response schemas are available at `http://127.0.0.1:8000/docs`. Mock controls are at `http://127.0.0.1:8001/docs`.

| Method and route | Input and result |
| --- | --- |
| `GET /health`, `GET /db-health` | Service and database readiness checks. |
| `POST /webhook` | JSON event with `event_id`, `event_type`, and object `payload`. Provide `X-Signature`. Returns `processed` or `already_processed`. |
| `POST /upload` | Multipart `file` with CSV or a JSON array. Returns accepted and quarantined record counts. |
| `POST /operator/investigations` | JSON with `endpoint_id`, `question`, and optional `delivery_id`. Returns observed evidence, likely cause, cited documentation, and any pending approval. |
| `POST /operator/retry-approvals/{approval_id}/approve` | Submit the reported `action` and `arguments`. Returns 202 for a newly queued retry, 409 for a mismatch, stale delivery, or reuse, and 410 for expiry. |
| `GET /operator/retry-approvals/{approval_id}` | Returns approval status and the delivery attempt result once recorded. |
| Mock `GET /health`, `GET /mode`, `POST /mode` | Inspect and change the mock receiver's behavior. |

The operator routes require the configured demo bearer token. Webhook signing verifies request integrity separately.

## Start the full stack with Docker Compose

With Docker Desktop running Linux containers, run this from the repository root:

```powershell
docker compose up --build --wait
docker compose ps
```

The API runs on `127.0.0.1:8000`, the mock on `127.0.0.1:8001`, and PostgreSQL on `127.0.0.1:5433`. Compose creates a dedicated `opspilot_demo` database on a new volume and the worker registers the mock during startup. The worker shares the mock's network namespace so it can use the explicit loopback HTTP option. The API and worker connect to PostgreSQL using the internal `db:5432` address.

The default credentials and signing keys are for local demos. Set `OPSPILOT_COMPOSE_POSTGRES_PASSWORD`, `OPSPILOT_COMPOSE_WEBHOOK_SECRET`, or `OPSPILOT_COMPOSE_MOCK_SECRET` before startup to override them. If a host port is in use, set `OPSPILOT_COMPOSE_API_PORT`, `OPSPILOT_COMPOSE_MOCK_PORT`, or `OPSPILOT_COMPOSE_DB_PORT` and adjust the local URLs in the examples.

Copy `Set-MockMode` and `Submit-DemoEvent` from the PowerShell helpers below to exercise the Compose stack. In the same window, set `$env:WEBHOOK_SECRET = 'local-demo-inbound-key'` or use your Compose override. Use this Compose version of `Show-Deliveries` with the scenario table:

```powershell
function Show-Deliveries {
    docker compose exec -T db psql -U opspilot_demo -d opspilot_demo -c 'SELECT e.event_id, d.status, a.attempt_number, a.outcome, a.status_code, dl.reason AS dead_letter_reason FROM deliveries d JOIN events e ON e.id = d.event_id LEFT JOIN delivery_attempts a ON a.delivery_id = d.id LEFT JOIN dead_letter_events dl ON dl.delivery_id = d.id ORDER BY d.id, a.attempt_number'
}
```

Run `docker compose stop` to stop the stack while retaining its database volume.

## Day 2 investigation and approval demo

Enable the operator API with a token in the PowerShell window that starts Compose. The token grants access to investigation and approval routes, so keep it with the operator.

```powershell
$env:OPSPILOT_OPERATOR_TOKEN = [guid]::NewGuid().ToString('N')
$env:COMPOSE_PROJECT_NAME = 'opspilot-day2'
docker compose up --build --wait
$scenarios = docker compose exec -T worker python -m app.seed_scenarios | ConvertFrom-Json
$scenarios | Format-Table
$api = 'http://127.0.0.1:8000'
$mock = 'http://127.0.0.1:8001'
$operatorHeaders = @{ Authorization = "Bearer $env:OPSPILOT_OPERATOR_TOKEN" }
```

The seeder creates three fixture endpoints, events, and deliveries in the Compose demo database. The ordinary worker generates the attempts. The expected causes are stored as evaluation data and kept outside the agent's inputs. The 401 fixture uses a deliberately mismatched signing secret. Fixture endpoints receive only their seeded deliveries. Rerunning the seed command returns the existing IDs. Use a fresh Compose project name for a clean replay, or set alternate host ports if the first project is still running.

Wait for the first attempt for each fixture, then ask the investigator for a report:

```powershell
foreach ($scenario in $scenarios) {
    $report = Invoke-RestMethod "$api/operator/investigations" -Method Post -Headers $operatorHeaders -ContentType 'application/json' -Body (@{
        endpoint_id = $scenario.endpoint_id
        delivery_id = $scenario.delivery_id
        question = 'Why is this customer endpoint failing?'
    } | ConvertTo-Json)
    $report | ConvertTo-Json -Depth 8
}
```

The report cites the observed status and relevant Markdown filenames. A 401 is evidence of an authentication failure, and a signing secret mismatch is a hypothesis until the receiver's configuration is checked. The agent recommends verifying the signing configuration before retrying. For retryable failures it waits for automatic retries to finish before requesting approval. Use the Compose `Show-Deliveries` query above to see when the 503 fixture is dead-lettered, typically within 100 seconds.

Once it is dead-lettered, request a fresh report and simulate recovery. Review the action and arguments before running the separate approval command:

```powershell
$scenario = $scenarios | Where-Object name -eq 'endpoint-down'
$report = Invoke-RestMethod "$api/operator/investigations" -Method Post -Headers $operatorHeaders -ContentType 'application/json' -Body (@{
    endpoint_id = $scenario.endpoint_id
    delivery_id = $scenario.delivery_id
    question = 'Is a manual retry appropriate after the endpoint recovers?'
} | ConvertTo-Json)
$report.approval | ConvertTo-Json -Depth 5
Invoke-RestMethod "$mock/scenarios/endpoint-down/mode" -Method Post -ContentType 'application/json' -Body '{"mode":"healthy"}'
```

```powershell
$approval = $report.approval
$approvalUrl = "$api/operator/retry-approvals/$($approval.approval_id)"
$action = @{ action = $approval.action; arguments = $approval.arguments } | ConvertTo-Json -Depth 5
Invoke-RestMethod "$approvalUrl/approve" -Method Post -Headers $operatorHeaders -ContentType 'application/json' -Body $action
Start-Sleep -Seconds 2
Invoke-RestMethod $approvalUrl -Headers $operatorHeaders | ConvertTo-Json -Depth 5
```

The backend checks the stored action, canonical SHA-256 argument hash, expiry, and observed attempt count. Approvals expire after 15 minutes and can be consumed once. Approval and queueing commit together, and the worker makes one extra attempt using the existing idempotency key. A status of `queued` or `in_progress` is provisional. Poll the approval URL until it is `completed`, then inspect the recorded HTTP status and `result.delivered`. Historical dead-letter metadata is retained after a successful manual retry. An interrupted manual attempt is recorded as failed and requires a new approval for another send.

The investigator works offline using delivery evidence and keyword search over the three files in `docs/`. If you explicitly configure both `OPSPILOT_OPENAI_API_KEY` and `OPSPILOT_INVESTIGATION_MODEL` before starting Compose, the same report can include a model-written explanation. The model receives the classified failure and documentation excerpts. The backend alone can approve, queue, execute, or verify a retry. There is no vector search or pgvector dependency.

### Measured investigation results

The investigation matched **3 of 3 seeded diagnoses** using attempts produced by the relay worker. This measures the three local fixtures.

| Observed failure | Expected and reported diagnosis | First citation |
| --- | --- | --- |
| HTTP 503 | Customer endpoint unavailable | [retries.md](docs/retries.md) |
| HTTP 401 | Authentication/signing secret mismatch | [authentication.md](docs/authentication.md) |
| HTTP 429 | Customer endpoint rate-limiting requests | [rate-limits.md](docs/rate-limits.md) |

The scenario tests inspect the actual tool calls. The investigator checks endpoint health, delivery history, and the failed attempt before searching documentation. An approval request is added for an eligible dead-lettered transient failure. The approval tests exercise the stored hash, modified arguments, expiry, concurrent use, and verified 200 and 503 retry outcomes.

## Run locally

In PowerShell, use the existing Python 3.13 `.venv`, or create one with `py -3.13 -m venv .venv` if needed.

```powershell
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe -m uvicorn app.main:app --no-access-log
```

Set `POSTGRES_HOST`, `POSTGRES_PORT`, `POSTGRES_DB`, `POSTGRES_USER`, `POSTGRES_PASSWORD`, and `WEBHOOK_SECRET` in your environment or a local `.env` file. Keep that file out of version control. The app creates its tables on startup.

## Ingestion behavior

- `POST /webhook` expects `X-Signature` in the form `sha256=<64 lowercase hex digits>`. Compute HMAC-SHA256 over the exact bytes you send. Invalid signatures return 401 and signed invalid events return 422. A retry of an existing event ID returns 200 with `already_processed`.
- `POST /upload` accepts multipart field `file`. JSON uploads must contain an array. CSV uploads use a header row and a JSON object in the `payload` column. Both formats require nonempty `event_id` and `event_type`. Invalid rows and duplicate IDs go to quarantine. The response counts only newly inserted events as accepted. Quarantine row numbers are one-based data record positions, excluding the CSV header and blank CSV rows.
- Invalid JSON files, unreadable CSV encoding, and CSV header syntax errors return 400. Malformed JSON inside a CSV payload is quarantined with its original field value. Quoted multiline CSV fields are supported. An unterminated quoted field can consume the rest of the file as one malformed record because the following record boundary is ambiguous.
- PostgreSQL enforces uniqueness on `event_id`. Both routes use `ON CONFLICT DO NOTHING RETURNING id` to distinguish an insert from a duplicate. Each upload commits event and quarantine rows together, and database failures roll back the batch.
- Each response includes a server-generated `X-Correlation-ID`. Match that UUID to `correlation_id` in the JSON logs. Application logs contain status, counts, and fixed error messages. Quarantine records retain rejected input in PostgreSQL for investigation.

## Outbound webhook relay

Start the API once to create the relay tables, then use a second PowerShell window with the same database settings.

```powershell
.\.venv\Scripts\python.exe -m app.worker add-endpoint
.\.venv\Scripts\python.exe -m app.worker run
```

Endpoint registration prompts for an HTTPS URL and signing secret without echoing them. Registration is an operator action. The database stores the secret, so restrict access to the endpoints table and database backups. Use `run --once` to poll for and process at most one due delivery.

Every newly accepted webhook or upload event queues one delivery for each enabled endpoint in the same transaction as the event. Duplicate and quarantined events do not create deliveries. The current service has one endpoint list shared by all accepted events. Registering a new endpoint does not replay historical events.

Workers claim one due row at a time with `FOR UPDATE SKIP LOCKED`. A claim commits an `in_progress` attempt and a 30-second lease before sending. During the HTTP call the worker holds the delivery row lock so another worker skips it. If the worker crashes, the lock is released and the attempt is marked `interrupted` when another worker recovers the delivery after the lease expires. Interrupted attempts count toward the retry limit.

The receiver gets a JSON event with `event_id`, `event_type`, and `payload`. Verify `X-Signature` as HMAC-SHA256 over the exact body bytes using the endpoint secret, and deduplicate using the stable `Idempotency-Key`. If a receiver accepts a request and the worker crashes before committing the result, the next attempt can send it again with the same key.

The worker records each attempt's timestamp, outcome, and HTTP status when available. A 2xx succeeds. Timeouts, transport errors, HTTP 408, 429, and 5xx are retried up to five total attempts. Exponential backoff uses base delays of 5, 10, 20, and 40 seconds, each with ±20% jitter. Other HTTP responses, disabled or invalid endpoints, and exhausted attempts go to `dead_letter_events`. The worker does not follow redirects or store response bodies.

## Demo with a local mock customer

Run the following from the repository root in PowerShell. Use an already provisioned, isolated `opspilot_test` database. Start that PostgreSQL instance if it is stopped, then set the host, port, username, and password for that instance below. These commands create a fresh demo schema, so repeated demos do not reuse old events or endpoints.

If you manage the isolated cluster with `pg_ctl`, point this command at its existing data directory:

```powershell
$testCluster = 'C:\path\to\isolated-postgres-data'
& 'C:\Program Files\PostgreSQL\18\bin\pg_ctl.exe' -D $testCluster -l "$testCluster\server.log" -o '-h 127.0.0.1 -p 55438' -w start
```

```powershell
$py = (Resolve-Path .\.venv\Scripts\python.exe).Path
$psql = 'C:\Program Files\PostgreSQL\18\bin\psql.exe'
$env:POSTGRES_HOST = '127.0.0.1'
$env:POSTGRES_PORT = '55438' # Replace with your isolated PostgreSQL port
$env:POSTGRES_DB = 'opspilot_test'
$env:POSTGRES_USER = 'opspilot_test'
$env:POSTGRES_PASSWORD = '' # Supply your isolated role password if required
$env:WEBHOOK_SECRET = 'local-demo-inbound-key'
$env:MOCK_CUSTOMER_SIGNING_SECRET = 'local-demo-customer-key'
$env:PGPASSWORD = $env:POSTGRES_PASSWORD
$dbArgs = @('-h', $env:POSTGRES_HOST, '-p', $env:POSTGRES_PORT, '-U', $env:POSTGRES_USER, '-d', $env:POSTGRES_DB, '-v', 'ON_ERROR_STOP=1')
$demoSchema = 'demo_' + [guid]::NewGuid().ToString('N')
& $psql @dbArgs -c "CREATE SCHEMA $demoSchema"
if ($LASTEXITCODE -ne 0) { throw 'Could not initialize the isolated demo schema' }
$env:PGOPTIONS = "-csearch_path=$demoSchema"

$apiProcess = Start-Process -FilePath $py -ArgumentList '-m uvicorn app.main:app --host 127.0.0.1 --port 8000 --no-access-log' -WindowStyle Hidden -PassThru
$mockProcess = Start-Process -FilePath $py -ArgumentList '-m uvicorn mock_customer.main:app --host 127.0.0.1 --port 8001 --no-access-log' -WindowStyle Hidden -PassThru
Start-Sleep -Seconds 2
Invoke-RestMethod 'http://127.0.0.1:8000/db-health'
Invoke-RestMethod 'http://127.0.0.1:8001/health'
& $py -m app.worker check-db
if ($LASTEXITCODE -ne 0) { throw 'The relay tables are not available in this demo schema' }
& $py -m app.worker add-endpoint --allow-loopback-http
```

Enter `http://127.0.0.1:8001/webhook` and `local-demo-customer-key` at the hidden prompts. Wait for the registration confirmation, then run:

```powershell
$workerProcess = Start-Process -FilePath $py -ArgumentList '-m app.worker run --allow-loopback-http' -WindowStyle Hidden -PassThru
```

The development flag is required both when registering and when sending to an HTTP endpoint. It accepts only the literal addresses `127.0.0.1` and `::1`. Normal registration and workers still require HTTPS. Run the mock on loopback with one Uvicorn worker. Its mode, counters, and deduplication keys live in memory and reset on restart.

If registration reports `Could not store the endpoint`, run `& $py -m app.worker check-db` in the same PowerShell window. For this demo it should report `opspilot_test`, your `demo_...` schema, and visible relay tables. Environment settings from the setup block apply to that window and the processes it launches. If the check reports `ingestion_db`, `public`, or missing tables, rerun the demo setup in one PowerShell window so that the API initializes the same isolated schema that registration and the worker use.

Visit [mock customer Swagger UI](http://127.0.0.1:8001/docs) to switch modes with `POST /mode` and inspect the processed and duplicate counters with `GET /mode`. You can also use these PowerShell helpers:

```powershell
function Set-MockMode($mode) {
    Invoke-RestMethod 'http://127.0.0.1:8001/mode' -Method Post -ContentType 'application/json' -Body (@{mode=$mode} | ConvertTo-Json -Compress)
}
function Submit-DemoEvent($id) {
    $json = [ordered]@{event_id=$id; event_type='demo.created'; payload=@{amount=5}} | ConvertTo-Json -Compress
    $body = [Text.Encoding]::UTF8.GetBytes($json)
    $hmac = [Security.Cryptography.HMACSHA256]::new([Text.Encoding]::UTF8.GetBytes($env:WEBHOOK_SECRET))
    try {
        $signature = 'sha256=' + [BitConverter]::ToString($hmac.ComputeHash($body)).Replace('-', '').ToLowerInvariant()
    } finally {
        $hmac.Dispose()
    }
    Invoke-RestMethod 'http://127.0.0.1:8000/webhook' -Method Post -ContentType 'application/json' -Headers @{'X-Signature'=$signature} -Body $body
}
function Show-Deliveries {
    & $psql @dbArgs -c 'SELECT e.event_id, d.status, a.attempt_number, a.outcome, a.status_code, dl.reason AS dead_letter_reason FROM deliveries d JOIN events e ON e.id = d.event_id LEFT JOIN delivery_attempts a ON a.delivery_id = d.id LEFT JOIN dead_letter_events dl ON dl.delivery_id = d.id ORDER BY d.id, a.attempt_number'
}
```

Run one scenario at a time, using a fresh event ID for each run:

| Scenario | Commands | Expected result in `Show-Deliveries` |
| --- | --- | --- |
| Success | `Set-MockMode healthy; Submit-DemoEvent demo-success; Start-Sleep 2; Show-Deliveries` | 200, delivered |
| Recovery after 500 | `Set-MockMode server_error; Submit-DemoEvent demo-500; Start-Sleep 2; Show-Deliveries; Set-MockMode healthy; Start-Sleep 8; Show-Deliveries` | 500 followed by 200 on the automatic retry |
| Retry exhaustion after 503 | `Set-MockMode unavailable; Submit-DemoEvent demo-503; Start-Sleep 100; Show-Deliveries` | Five 503 attempts, then dead-lettered |
| Permanent failure | `Set-MockMode unauthorized; Submit-DemoEvent demo-401; Start-Sleep 2; Show-Deliveries` | One 401, then dead-lettered |
| Rate limit and recovery | `Set-MockMode rate_limited; Submit-DemoEvent demo-429; Start-Sleep 2; Show-Deliveries; Set-MockMode healthy; Start-Sleep 8; Show-Deliveries` | 429 followed by 200. The mock returns `Retry-After: 10`; the worker uses its jittered backoff schedule. |
| Timeout and recovery | `Set-MockMode timeout; Submit-DemoEvent demo-timeout; Start-Sleep 7; Show-Deliveries; Set-MockMode healthy; Start-Sleep 8; Show-Deliveries` | First attempt is `timeout` with no HTTP status; the retry receives 200. |

To see the receiver's counters, run `Invoke-RestMethod 'http://127.0.0.1:8001/mode'`. The receiver authenticates the raw body before processing it. Repeating a successful request with the same body and `Idempotency-Key` returns `duplicate` and does not increment `processed`. Reusing that key for different signed bytes returns 409.

Stop the demo processes with `Stop-Process -Id $workerProcess.Id,$mockProcess.Id,$apiProcess.Id`. The demo data remains in its isolated schema.

## Run tests

Use a dedicated PostgreSQL database named `opspilot_test`. If it does not exist, arrange for it to be provisioned before running the integration suite. The test role needs permission to connect and create schemas. Each integration test creates a fresh schema in that database and leaves it in place. The suite rejects the configured development database name and performs no database writes when the test URL is unset.

```powershell
$env:OPSPILOT_TEST_DATABASE_URL = 'postgresql+psycopg://TEST_USER:TEST_PASSWORD@127.0.0.1:TEST_PORT/opspilot_test'
.\.venv\Scripts\python.exe -m pytest -q
```

Replace the placeholder values with your isolated database connection details. If the URL is unset, pytest skips the PostgreSQL cases and runs the request validation, logging, and mock customer tests. The suite includes both focused relay tests with mocked HTTP and an end-to-end test with real loopback HTTP servers, a separate worker process, and PostgreSQL. Run the latter on its own with `.\.venv\Scripts\python.exe -m pytest -q tests/test_local_relay.py`.

To repeat the three-scenario measurement against isolated PostgreSQL:

```powershell
.\.venv\Scripts\python.exe -m pytest -q tests/test_investigation.py -k seed_scenarios
```

## Known limitations

- **Tenant isolation is deferred.** Enabled customer endpoints share a routing list. The three seeded endpoints are reserved for their fixtures.
- **Application user authentication is deferred.** The demo uses a single shared operator token. It has no user accounts, roles, or per-person approval identity. Webhook HMAC signing is implemented.
- **The measured agent result covers three seeded cases.** The default investigator uses rules and keyword retrieval. Optional model-written explanations do not control diagnosis, approval, or execution. Vector retrieval and pgvector are deferred.
- **Delivery is at least once.** Receivers must deduplicate by idempotency key. The mock keeps its keys in memory, so a restart clears them. The relay uses its own jittered backoff for 429 responses.
