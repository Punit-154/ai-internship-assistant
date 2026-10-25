"""Pytest configuration and reusable fixtures.

Configures an isolated in-memory SQLite database using StaticPool
so tests never touch disk or pollute real databases.
"""

import pytest
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine

from src.database import init_db


@pytest.fixture(name="test_engine")
def fixture_test_engine():
    """Create an isolated in-memory SQLite engine for the test."""
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    init_db(engine)
    yield engine
    SQLModel.metadata.drop_all(engine)


@pytest.fixture(name="session")
def fixture_session(test_engine):
    """Provide a transactional session bound to the in-memory test database."""
    with Session(test_engine) as session:
        yield session
