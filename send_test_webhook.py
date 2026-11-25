
import hashlib
import hmac
import json
import os

import httpx
from dotenv import load_dotenv

load_dotenv()

secret = os.getenv("WEBHOOK_SECRET")

event = {
    "event_id": "evt_002",
    "event_type": "payment.success",
    "payload": {
        "amount": 9,
        "currency": "INR"
    }
}

# Serialize event into bytes
body = json.dumps(event).encode("utf-8")

# Generate HMAC signature
signature = hmac.new(
    secret.encode("utf-8"),
    body,
    hashlib.sha256
).hexdigest()

headers = {
    "X-Signature": f"sha256={signature}",
    "Content-Type": "application/json"
}



response = httpx.post(
    "http://127.0.0.1:8000/webhook",
    content=body,
    headers=headers
)

print("Status:", response.status_code)
print("Response:", response.json())
