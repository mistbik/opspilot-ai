import os
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.engine import make_url
from sqlalchemy.schema import CreateSchema

from app import database, main, security


@pytest.fixture(autouse=True)
def signing_secret(monkeypatch):
    secret = "pytest-signing-secret"
    monkeypatch.setattr(security, "WEBHOOK_SECRET", secret)
    return secret


@pytest.fixture
def client(monkeypatch):
    def block_database(*args):
        raise AssertionError("This test must not access a database")

    monkeypatch.setattr(main, "create_tables", lambda: None)
    monkeypatch.setattr(main, "persist_webhook", block_database)
    monkeypatch.setattr(main, "engine", None)
    with TestClient(main.app) as test_client:
        yield test_client


@pytest.fixture
def test_engine():
    dsn = os.getenv("OPSPILOT_TEST_DATABASE_URL")
    if not dsn:
        pytest.skip("Set OPSPILOT_TEST_DATABASE_URL to enable PostgreSQL integration tests")

    url = make_url(dsn)
    if url.drivername != "postgresql+psycopg" or url.database != "opspilot_test":
        pytest.fail("Use a PostgreSQL psycopg URL targeting the dedicated opspilot_test database")
    if url.database == database.database_url.database:
        pytest.fail("The integration suite cannot use the configured development database")

    schema = f"test_{uuid4().hex}"
    engine = create_engine(url, connect_args={"options": f"-csearch_path={schema}"})
    try:
        with engine.begin() as connection:
            if connection.exec_driver_sql("SELECT current_database()").scalar_one() != "opspilot_test":
                pytest.fail("PostgreSQL did not connect to the dedicated opspilot_test database")
            connection.execute(CreateSchema(schema))
        yield engine
    finally:
        engine.dispose()


@pytest.fixture
def postgres_client(test_engine, monkeypatch):
    monkeypatch.setattr(database, "engine", test_engine)
    monkeypatch.setattr(main, "engine", test_engine)
    with TestClient(main.app) as test_client:
        yield test_client
