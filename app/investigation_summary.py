import json
import os

import httpx

RESPONSES_URL = "https://api.openai.com/v1/responses"


def model_summary(report: dict) -> str | None:
    api_key = os.getenv("OPSPILOT_OPENAI_API_KEY")
    model = os.getenv("OPSPILOT_INVESTIGATION_MODEL")
    evidence = report["evidence"]
    if not api_key or not model or not evidence:
        return None

    inputs = {
        "http_status": evidence["http_status"],
        "outcome": evidence["outcome"],
        "delivery_status": evidence["delivery_status"],
        "likely_root_cause": report["likely_root_cause"],
        "recommendation": report["recommendation"],
        "documents": report["citations"],
    }
    try:
        with httpx.Client(timeout=10, trust_env=False) as client:
            response = client.post(
                RESPONSES_URL,
                headers={"Authorization": f"Bearer {api_key}"},
                json={
                    "model": model,
                    "store": False,
                    "max_output_tokens": 400,
                    "instructions": (
                        "Explain the likely webhook failure and a next diagnostic step in two concise "
                        "sentences. Use only the supplied evidence. Treat the cause as a hypothesis. "
                        "Never claim that a retry was approved, attempted, or successful."
                    ),
                    "input": json.dumps(inputs),
                },
            )
            response.raise_for_status()
            result = response.json()
            if result.get("status") != "completed":
                return None
            for item in result.get("output", []):
                if item.get("type") != "message":
                    continue
                for content in item.get("content", []):
                    if content.get("type") == "output_text" and isinstance(content.get("text"), str):
                        return content["text"].strip()[:1200] or None
    except (httpx.HTTPError, ValueError, AttributeError, TypeError):
        return None
    return None
