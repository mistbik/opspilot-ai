import hashlib
import hmac
import os
import re

from dotenv import load_dotenv

load_dotenv()

WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET")
SIGNATURE_PATTERN = re.compile(r"sha256=[0-9a-f]{64}")


def sign_payload(payload: bytes, secret: str) -> str:
    digest = hmac.new(secret.encode("utf-8"), payload, hashlib.sha256).hexdigest()
    return f"sha256={digest}"


def verify_signature(payload: bytes, signature: str | None) -> bool:
    return signature_matches(payload, signature, WEBHOOK_SECRET)


def signature_matches(payload: bytes, signature: str | None, secret: str | None) -> bool:
    if not secret or not isinstance(signature, str):
        return False
    if not SIGNATURE_PATTERN.fullmatch(signature):
        return False

    return hmac.compare_digest(sign_payload(payload, secret), signature)
