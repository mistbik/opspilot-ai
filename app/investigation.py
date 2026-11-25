from typing import Protocol


class InvestigationTools(Protocol):
    def get_endpoint_health(self, endpoint_id: int) -> dict: ...
    def get_delivery_history(self, endpoint_id: int) -> list[dict]: ...
    def get_delivery_failure(self, endpoint_id: int, delivery_id: int) -> dict | None: ...
    def search_documentation(self, query: str) -> list[dict]: ...
    def request_retry_approval(self, delivery_id: int) -> dict: ...


def investigate(
    tools: InvestigationTools, endpoint_id: int, question: str, delivery_id: int | None
) -> dict:
    health = tools.get_endpoint_health(endpoint_id)
    history = tools.get_delivery_history(endpoint_id)
    if delivery_id is None:
        delivery_id = next(
            (row["delivery_id"] for row in history if row["delivery_status"] != "delivered"
             and row["outcome"] != "in_progress"),
            None,
        )
    failure = tools.get_delivery_failure(endpoint_id, delivery_id) if delivery_id is not None else None
    root_cause = "Insufficient evidence"
    recommendation = "Check the endpoint and look for a completed failed delivery attempt."
    topic = "retry policy delivery"
    eligible = False

    if failure:
        code = failure["http_status"]
        if code == 401:
            root_cause = "Authentication/signing secret mismatch"
            recommendation = "Verify the receiver's signing key and raw-body HMAC validation before retrying."
            topic = "401 authentication signing secret"
        elif code == 429:
            root_cause = "Customer endpoint rate-limiting requests"
            recommendation = "Confirm the throttling window has cleared before approving a retry."
            topic = "429 rate limits throttling"
            eligible = True
        elif code == 503:
            root_cause = "Customer endpoint unavailable"
            recommendation = "Confirm the receiver has recovered before approving a retry."
            topic = "503 endpoint unavailable retries"
            eligible = True
        elif failure["outcome"] in {"timeout", "transport_error", "interrupted"} or (
            code is not None and 500 <= code < 600
        ):
            root_cause = "Transient delivery failure"
            recommendation = "Check receiver availability before approving a retry."
            eligible = True
        else:
            root_cause = "Receiver or endpoint configuration rejected delivery"
            recommendation = "Inspect the recorded status and correct the endpoint before retrying."

    citations = tools.search_documentation(f"{topic} {topic} {question}")
    approval = None
    if eligible and health["enabled"] and failure["delivery_status"] == "dead_lettered":
        approval = tools.request_retry_approval(failure["delivery_id"])
    elif eligible and failure["delivery_status"] == "pending":
        recommendation = "Monitor the delivery while the worker handles its scheduled attempts."

    return {
        "endpoint": health,
        "delivery_id": delivery_id,
        "likely_root_cause": root_cause,
        "confidence": "medium" if failure else "low",
        "evidence": failure,
        "recent_attempts": history,
        "citations": citations,
        "recommendation": recommendation,
        "approval": approval,
    }
