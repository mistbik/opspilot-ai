import argparse
import logging
import os
import sys
import time
from getpass import getpass

import httpx
import structlog
from sqlalchemy import func, inspect, select, text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.database import engine
from app.models import DeadLetterEvent, Delivery, DeliveryAttempt, Endpoint, Event
from app.relay import HTTP_TIMEOUT_SECONDS, endpoint_url, process_next_delivery

DATABASE_HINTS = {
    "42P01": "A relay table is missing. Start the API with the same database settings and PGOPTIONS.",
    "3F000": "The schema is missing. Check PGOPTIONS and start the API in the intended schema.",
    "42501": "The PostgreSQL role lacks permission for this operation.",
    "28P01": "PostgreSQL rejected the password. Check POSTGRES_USER and POSTGRES_PASSWORD.",
    "3D000": "The configured database does not exist. Check POSTGRES_DB.",
}
MOCK_ENDPOINT_URL = "http://127.0.0.1:8001/webhook"


def register_mock_endpoint(secret: str) -> None:
    with Session(engine) as session, session.begin():
        # Serialize demo registration across worker restarts or concurrent starts.
        session.execute(select(func.pg_advisory_xact_lock(610332750)))
        endpoint = session.scalars(
            select(Endpoint).where(Endpoint.url == MOCK_ENDPOINT_URL).order_by(Endpoint.id)
        ).first()
        if endpoint is None:
            session.add(Endpoint(url=MOCK_ENDPOINT_URL, signing_secret=secret))
        else:
            endpoint.signing_secret = secret
            endpoint.enabled = True


def database_hint(error: SQLAlchemyError) -> str:
    original = getattr(error, "orig", None)
    state = getattr(original, "sqlstate", None)
    if state in DATABASE_HINTS:
        return DATABASE_HINTS[state]
    # libpq connection failures can arrive without a SQLSTATE.
    message = str(original).lower()
    if "password authentication failed" in message or "no password supplied" in message:
        return DATABASE_HINTS["28P01"]
    if "does not exist" in message and "database" in message:
        return DATABASE_HINTS["3D000"]
    if "connection refused" in message or "timeout expired" in message:
        return "PostgreSQL is unreachable. Start the database and check POSTGRES_HOST and POSTGRES_PORT."
    return "Check the database connection and run 'python -m app.worker check-db'."


def check_database(*, require_mock: bool = False) -> int:
    try:
        with engine.connect() as connection:
            database, schema = connection.execute(
                text("SELECT current_database(), current_schema()")
            ).one()
            print(f"Connected to database {database}, schema {schema or '(none)'}")
            inspector = inspect(connection)
            missing = [
                model.__tablename__
                for model in (Event, Endpoint, Delivery, DeliveryAttempt, DeadLetterEvent)
                if not inspector.has_table(model.__tablename__)
            ]
            if require_mock and not missing and not connection.scalar(select(
                select(Endpoint.id).where(
                    Endpoint.url == MOCK_ENDPOINT_URL, Endpoint.enabled.is_(True)
                ).exists()
            )):
                print("The mock endpoint is not registered.", file=sys.stderr)
                return 1
    except SQLAlchemyError as error:
        print(database_hint(error), file=sys.stderr)
        return 1
    if missing:
        print(f"Missing tables: {', '.join(missing)}. {DATABASE_HINTS['42P01']}", file=sys.stderr)
        return 1
    print("Relay tables are visible.")
    return 0


def run_worker(once: bool, *, allow_loopback_http: bool = False) -> int:
    # HTTPX INFO logs contain the endpoint URL, which may contain sensitive data.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    log = structlog.wrap_logger(
        structlog.PrintLogger(), processors=[
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso", utc=True),
            structlog.processors.JSONRenderer(),
        ],
    )
    with httpx.Client(timeout=httpx.Timeout(HTTP_TIMEOUT_SECONDS), trust_env=False) as client:
        while True:
            try:
                with Session(engine) as session:
                    processed = process_next_delivery(
                        session, client, allow_loopback_http=allow_loopback_http
                    )
            except SQLAlchemyError as error:
                log.error("relay_database_failure", hint=database_hint(error))
                if once:
                    return 1
                processed = False
            if once:
                return 0
            if not processed:
                time.sleep(1)


def main() -> int:
    parser = argparse.ArgumentParser(description="Manage the outbound webhook relay")
    commands = parser.add_subparsers(dest="command", required=True)
    check = commands.add_parser("check-db", help="Check database connectivity and relay tables without writing")
    check.add_argument("--require-mock", action="store_true", help="Also check demo endpoint registration")
    run = commands.add_parser("run", help="Poll for due deliveries")
    run.add_argument("--once", action="store_true", help="Process at most one due delivery")
    run.add_argument("--register-mock", action="store_true", help="Register the local demo receiver")
    add = commands.add_parser("add-endpoint", help="Register an enabled endpoint interactively")
    for command in (run, add):
        command.add_argument(
            "--allow-loopback-http", action="store_true",
            help="Development only: allow HTTP to 127.0.0.1 or ::1",
        )
    args = parser.parse_args()

    if args.command == "check-db":
        return check_database(require_mock=args.require_mock)

    if args.command == "run":
        if args.register_mock:
            secret = os.getenv("MOCK_CUSTOMER_SIGNING_SECRET")
            if not args.allow_loopback_http or not secret:
                parser.error("--register-mock requires --allow-loopback-http and MOCK_CUSTOMER_SIGNING_SECRET")
            try:
                register_mock_endpoint(secret)
            except SQLAlchemyError as error:
                parser.exit(1, f"Could not register the mock endpoint. {database_hint(error)}\n")
        try:
            return run_worker(args.once, allow_loopback_http=args.allow_loopback_http)
        except KeyboardInterrupt:
            return 0

    try:
        prompt = "Endpoint URL: " if args.allow_loopback_http else "Endpoint HTTPS URL: "
        url = endpoint_url(getpass(prompt), allow_loopback_http=args.allow_loopback_http)
    except (ValueError, httpx.InvalidURL):
        message = "Use an HTTPS endpoint URL without user info or a fragment"
        if args.allow_loopback_http:
            message += ", or use HTTP with 127.0.0.1 or ::1"
        parser.error(message)
    secret = getpass("Endpoint signing secret: ")
    if not secret:
        parser.error("A signing secret is required")
    try:
        with Session(engine) as session, session.begin():
            endpoint = Endpoint(url=str(url), signing_secret=secret)
            session.add(endpoint)
            session.flush()
            endpoint_id = endpoint.id
    except SQLAlchemyError as error:
        parser.exit(1, f"Could not store the endpoint. {database_hint(error)}\n")
    print(f"Endpoint {endpoint_id} registered")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
