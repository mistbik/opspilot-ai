import json
import os
from sys import exit

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.database import engine
from app.models import Delivery, Endpoint, Event, InvestigationScenario

SCENARIOS = (
    ("endpoint-down", 503, "Customer endpoint unavailable"),
    ("expired-signing-secret", 401, "Authentication/signing secret mismatch"),
    ("rate-limiting", 429, "Customer endpoint rate-limiting requests"),
)


def seed_scenarios(session: Session, mock_secret: str) -> list[dict]:
    output = []
    with session.begin():
        session.execute(select(func.pg_advisory_xact_lock(610332751)))
        for name, status, cause in SCENARIOS:
            scenario = session.get(InvestigationScenario, name)
            if scenario is None:
                endpoint = Endpoint(
                    url=f"http://127.0.0.1:8001/scenarios/{name}/webhook",
                    signing_secret=f"{mock_secret}-expired" if status == 401 else mock_secret,
                )
                event = Event(event_id=f"day2-scenario-{name}", event_type="demo.investigation", payload={})
                session.add_all([endpoint, event])
                session.flush()
                delivery = Delivery(event_id=event.id, endpoint_id=endpoint.id)
                session.add(delivery)
                session.flush()
                scenario = InvestigationScenario(
                    name=name, endpoint_id=endpoint.id, delivery_id=delivery.id,
                    expected_status=status, expected_root_cause=cause,
                )
                session.add(scenario)
            output.append({
                "name": scenario.name, "endpoint_id": scenario.endpoint_id,
                "delivery_id": scenario.delivery_id, "expected_status": scenario.expected_status,
                "expected_root_cause": scenario.expected_root_cause,
            })
    return output


def main() -> None:
    secret = os.getenv("MOCK_CUSTOMER_SIGNING_SECRET")
    if engine.url.database != "opspilot_demo" or not secret:
        exit("Run the seeder in the isolated Compose demo worker.")
    with Session(engine) as session:
        print(json.dumps(seed_scenarios(session, secret), indent=2))


if __name__ == "__main__":
    main()
