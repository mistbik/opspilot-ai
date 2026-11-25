# Retry policy

OpsPilot automatically retries timeouts, transport errors, HTTP 408, HTTP 429, and HTTP 5xx. An HTTP 503 typically means the customer endpoint is unavailable. Check the receiver's health and dependencies before replaying a failed event.

There are five automatic attempts with jittered exponential backoff. Permanent failures and exhausted attempts enter the dead-letter table. A dead-lettered delivery can receive one additional manual attempt per approved request. The retry preserves the original idempotency key. Confirm the endpoint has recovered before approving.
