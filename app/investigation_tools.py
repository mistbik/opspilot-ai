import re
from pathlib import Path

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.approvals import approval_details, request_retry_approval
from app.models import Delivery, DeliveryAttempt, Endpoint

DOCUMENTS = Path(__file__).resolve().parents[1] / "docs"


def attempt_evidence(delivery: Delivery, attempt: DeliveryAttempt) -> dict:
    return {
        "delivery_id": delivery.id, "delivery_status": delivery.status,
        "attempt_number": attempt.attempt_number, "attempted_at": attempt.attempted_at,
        "http_status": attempt.status_code, "outcome": attempt.outcome,
    }


class ServiceTools:
    def __init__(self, session: Session):
        self.session = session

    def get_endpoint_health(self, endpoint_id: int) -> dict:
        endpoint = self.session.get(Endpoint, endpoint_id)
        if endpoint is None:
            raise HTTPException(404, "Endpoint not found")
        last = self.session.scalar(
            select(DeliveryAttempt).join(Delivery)
            .where(Delivery.endpoint_id == endpoint_id, DeliveryAttempt.outcome != "in_progress")
            .order_by(DeliveryAttempt.id.desc()).limit(1)
        )
        return {
            "endpoint_id": endpoint.id, "enabled": endpoint.enabled,
            "last_observed_status": last.status_code if last else None,
            "last_observed_outcome": last.outcome if last else None,
        }

    def get_delivery_history(self, endpoint_id: int) -> list[dict]:
        rows = self.session.execute(
            select(Delivery, DeliveryAttempt).join(DeliveryAttempt)
            .where(Delivery.endpoint_id == endpoint_id)
            .order_by(DeliveryAttempt.id.desc()).limit(20)
        )
        return [attempt_evidence(delivery, attempt) for delivery, attempt in rows]

    def get_delivery_failure(self, endpoint_id: int, delivery_id: int) -> dict | None:
        delivery = self.session.get(Delivery, delivery_id)
        if delivery is None or delivery.endpoint_id != endpoint_id:
            raise HTTPException(404, "Delivery not found for endpoint")
        if delivery.status == "delivered":
            return None
        attempt = self.session.scalar(
            select(DeliveryAttempt).where(
                DeliveryAttempt.delivery_id == delivery_id,
                DeliveryAttempt.outcome != "in_progress",
            ).order_by(DeliveryAttempt.attempt_number.desc()).limit(1)
        )
        return attempt_evidence(delivery, attempt) if attempt else None

    def search_documentation(self, query: str) -> list[dict]:
        terms = re.findall(r"[a-z0-9]+", query.lower())
        matches = []
        for path in sorted(DOCUMENTS.glob("*.md")):
            content = path.read_text(encoding="utf-8")
            tokens = set(re.findall(r"[a-z0-9]+", content.lower()))
            score = sum(term in tokens for term in terms)
            if score:
                matches.append((score, path.name, content))
        matches.sort(key=lambda match: (-match[0], match[1]))
        return [
            {"filename": filename, "excerpt": content.split("\n\n")[1].strip()}
            for _, filename, content in matches[:2]
        ]

    def request_retry_approval(self, delivery_id: int) -> dict:
        return approval_details(self.session, request_retry_approval(self.session, delivery_id))
