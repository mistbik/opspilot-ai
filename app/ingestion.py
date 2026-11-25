import csv
import io
import json
from dataclasses import dataclass

from pydantic import ValidationError
from sqlalchemy import literal, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from app.models import Delivery, Endpoint, Event, InvestigationScenario, QuarantinedRecord
from app.schemas import WebhookEvent


@dataclass(frozen=True)
class InvalidCSVRow:
    raw_data: object
    reason: str


def insert_event(session: Session, event: WebhookEvent) -> bool:
    statement = (
        insert(Event)
        .values(event_id=event.event_id, event_type=event.event_type, payload=event.payload)
        .on_conflict_do_nothing(index_elements=[Event.event_id])
        .returning(Event.id)
    )
    event_id = session.execute(statement).scalar_one_or_none()
    if event_id is None:
        return False

    endpoints = select(literal(event_id), Endpoint.id).where(
        Endpoint.enabled.is_(True),
        ~select(InvestigationScenario.endpoint_id)
        .where(InvestigationScenario.endpoint_id == Endpoint.id).exists(),
    )
    session.execute(insert(Delivery).from_select(["event_id", "endpoint_id"], endpoints))
    return True


def process_records(records: list, session: Session) -> dict[str, int]:
    accepted = 0
    try:
        for index, record in enumerate(records, start=1):
            if isinstance(record, InvalidCSVRow):
                raw_data, reason = record.raw_data, record.reason
            else:
                raw_data = record
                try:
                    event = WebhookEvent.model_validate(record)
                except ValidationError as error:
                    reason = str(error)
                else:
                    if insert_event(session, event):
                        accepted += 1
                        continue
                    reason = "Duplicate event_id"

            session.add(
                QuarantinedRecord(
                    row_number=index,
                    raw_data=json.dumps(raw_data),
                    error_reason=reason,
                )
            )

        session.commit()
    except Exception:
        session.rollback()
        raise

    return {"accepted": accepted, "rejected": len(records) - accepted}


def parse_csv(content: bytes) -> list:
    lines = io.StringIO(content.decode("utf-8-sig"), newline="").readlines()
    reader = csv.reader(lines, strict=True)
    try:
        headers = next(reader, [])
    except csv.Error as error:
        raise ValueError("Invalid CSV header") from error
    records = []
    while True:
        start = reader.line_num
        try:
            values = next(reader)
        except StopIteration:
            break
        except csv.Error:
            records.append(InvalidCSVRow("".join(lines[start:reader.line_num]), "Invalid CSV row"))
            continue

        if not values:
            continue
        if len(values) != len(headers):
            records.append(InvalidCSVRow(values, "Incorrect CSV column count"))
            continue

        row = dict(zip(headers, values))
        if row.get("payload"):
            try:
                row["payload"] = json.loads(row["payload"])
            except (ValueError, RecursionError):
                records.append(InvalidCSVRow(row, "Invalid JSON in CSV payload"))
                continue
        records.append(row)
    return records


def parse_json(content: bytes) -> list:
    data = json.loads(content)
    if not isinstance(data, list):
        raise ValueError("JSON file must contain a list of records")
    return data
