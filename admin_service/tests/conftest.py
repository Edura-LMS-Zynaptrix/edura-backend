"""Fixtures for admin_service: an empty SQLite database wired into the app, and auth headers."""

import event_consumer
import jwt
import people
import pytest
from database import Base, get_db
from fastapi.testclient import TestClient
from main import app
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from shared import events as ev
from shared.auth import ALGORITHM, SECRET_KEY


def auth(role: str, sub: int) -> dict:
    return {
        "Authorization": "Bearer "
        + jwt.encode({"sub": str(sub), "role": role}, SECRET_KEY, algorithm=ALGORITHM)
    }


ADMIN = auth("admin", 1)
STUDENT = auth("student", 7)
OTHER_STUDENT = auth("student", 8)


@pytest.fixture()
def session_factory(monkeypatch):
    """An empty database; routes, the name lookup and the event consumer all use it."""
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(bind=engine)
    factory = sessionmaker(bind=engine, autoflush=False)
    monkeypatch.setattr(people, "SessionLocal", factory)
    monkeypatch.setattr(event_consumer, "SessionLocal", factory)
    yield factory
    Base.metadata.drop_all(bind=engine)
    engine.dispose()


@pytest.fixture()
def db(session_factory):
    session = session_factory()
    yield session
    session.close()


@pytest.fixture()
def client(session_factory):
    def override_get_db():
        session = session_factory()
        try:
            yield session
        finally:
            session.close()

    app.dependency_overrides[get_db] = override_get_db
    # Not used as a context manager, so startup (create_all + RabbitMQ) never runs.
    yield TestClient(app)
    app.dependency_overrides.clear()


@pytest.fixture()
def add_user(session_factory):
    """Register a person the way auth_service and user_service announce them."""

    def _add(user_id: int, email: str | None = None, full_name: str | None = None):
        event_consumer.handle(
            ev.AUTH_USER_REGISTERED,
            {"user_id": user_id, "email": email, "role": "student"},
        )
        if full_name:
            first, _, last = full_name.partition(" ")
            event_consumer.handle(
                ev.USER_PROFILE_UPSERTED,
                {
                    "user_id": user_id,
                    "role": "student",
                    "full_name": full_name,
                    "first_name": first,
                    "last_name": last,
                },
            )

    return _add
