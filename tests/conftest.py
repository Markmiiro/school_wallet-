# ================================================
# tests/conftest.py
# ------------------------------------------------
# Shared fixtures for the money-critical test suite.
#
# SAFETY: this suite must never touch the real/live Postgres database.
#   - By default each test gets a fresh in-memory SQLite database via a
#     dependency override on app.database.get_db.
#   - Optionally, set TEST_DATABASE_URL to point at a THROWAWAY local
#     Postgres database (never the app's real DATABASE_URL) to confirm
#     behavior that SQLite can't reproduce — e.g. row-level locking via
#     with_for_update(), which SQLAlchemy silently no-ops on SQLite.
#     Example: a local db created with `createdb school_wallet_test`,
#     run as: TEST_DATABASE_URL=postgresql://supreme@localhost/school_wallet_test
#              ./venv/bin/python -m pytest tests/
#   - We build a *standalone* FastAPI app that includes only the
#     routers under test, instead of importing app.main.app. main.py
#     registers an on_event("startup") hook that calls
#     test_connection() and create_tables() against the real
#     DATABASE_URL from .env — importing/running that in tests would
#     hit the live Railway database, which CLAUDE.md forbids. Building
#     our own app sidesteps that hook entirely while still exercising
#     the real router/model code.
# ================================================

import os

# Must be set before app.auth is imported (it raises at import time if
# SECRET_KEY is missing). This is a throwaway value for signing test
# JWTs only — never the real SECRET_KEY.
os.environ.setdefault("SECRET_KEY", "test-only-secret-not-for-production")
# Ensures app/momo.py's _is_test_mode() short-circuits to fake Yo calls.
os.environ.setdefault("APP_ENV", "development")

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.database import Base, get_db
from app import models
from app.auth import create_access_token, hash_pin

# Importing this registers PendingUssdRegistration (defined in ussd.py, on
# the same Base) with Base.metadata *before* any test's create_all() runs.
# webhook.py imports it too, but we need it registered ahead of the first
# db_session fixture, not just whenever webhook happens to be imported.
from app.routes import ussd as _ussd_module  # noqa: F401

TEST_DATABASE_URL = os.environ.get("TEST_DATABASE_URL")
if TEST_DATABASE_URL and "railway" in TEST_DATABASE_URL.lower():
    # Belt-and-braces: never let this suite run against anything that
    # looks like the production Railway database, however it got there.
    raise RuntimeError(
        "TEST_DATABASE_URL looks like it points at Railway — refusing to "
        "run tests against it. Use a throwaway local Postgres database."
    )


# ── Fresh database per test: in-memory SQLite by default, or a
#    throwaway Postgres database when TEST_DATABASE_URL is set ──
@pytest.fixture()
def db_session():
    if TEST_DATABASE_URL:
        engine = create_engine(TEST_DATABASE_URL)
    else:
        engine = create_engine(
            "sqlite:///:memory:",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
    Base.metadata.drop_all(bind=engine)  # clean slate even after a crashed prior run
    Base.metadata.create_all(bind=engine)
    TestingSessionLocal = sessionmaker(
        autocommit=False, autoflush=False, bind=engine
    )
    session = TestingSessionLocal()
    session._test_engine = engine
    session._test_sessionmaker = TestingSessionLocal
    try:
        yield session
    finally:
        session.close()
        engine.dispose()


# ── Minimal app: only the routers the money-critical tests need ──
def _build_test_app():
    from app.routes import payments, tuckshop, merchants, webhook, auth as auth_routes, students

    app = FastAPI()
    app.include_router(payments.router, prefix="/payments", tags=["Payments"])
    app.include_router(tuckshop.router, prefix="/tuckshop", tags=["Tuck Shop"])
    app.include_router(merchants.router, prefix="/merchants", tags=["Merchants"])
    app.include_router(webhook.router, prefix="/webhook", tags=["Webhook"])
    app.include_router(auth_routes.router, prefix="/auth", tags=["Authentication"])
    app.include_router(students.router, prefix="/students", tags=["Students"])
    return app


@pytest.fixture()
def client(db_session):
    app = _build_test_app()

    def override_get_db():
        session = db_session._test_sessionmaker()
        try:
            yield session
        finally:
            session.close()

    app.dependency_overrides[get_db] = override_get_db

    with TestClient(app) as c:
        yield c

    app.dependency_overrides.clear()


# ── Mute outbound SMS in every test (no real gateway calls) ──
@pytest.fixture(autouse=True)
def mock_sms(monkeypatch):
    calls = {"payment_alert": [], "low_balance": []}

    def fake_payment_alert(**kwargs):
        calls["payment_alert"].append(kwargs)

    def fake_low_balance(**kwargs):
        calls["low_balance"].append(kwargs)

    monkeypatch.setattr("app.routes.payments.sms_payment_alert", fake_payment_alert)
    monkeypatch.setattr("app.routes.payments.sms_low_balance_alert", fake_low_balance)
    return calls


# ── Domain fixtures ───────────────────────────────────
@pytest.fixture()
def school(db_session):
    s = models.School(name="Test School", location="Kampala")
    db_session.add(s)
    db_session.commit()
    db_session.refresh(s)
    return s


@pytest.fixture()
def parent_user(db_session):
    u = models.User(
        name="Test Parent",
        phone="256700111222",
        role="parent",
        pin_hash=hash_pin("1234"),
    )
    db_session.add(u)
    db_session.commit()
    db_session.refresh(u)
    return u


@pytest.fixture()
def merchant(db_session, school):
    m = models.Merchant(
        name="Test Tuck Shop",
        school_id=school.id,
        momo_phone="256700333444",
        is_active=True,
    )
    db_session.add(m)
    db_session.commit()
    db_session.refresh(m)
    return m


def make_student_with_wallet(
    db_session, school, parent, *, balance=10000, daily_limit=20000,
    wallet_active=True, tag_uid="A1B2C3D4",
):
    student = models.Student(
        name="Test Student", school_id=school.id, parent_id=parent.id,
    )
    db_session.add(student)
    db_session.flush()

    wallet = models.Wallet(
        student_id=student.id,
        balance=balance,
        is_active=wallet_active,
        daily_limit=daily_limit,
    )
    db_session.add(wallet)

    nfc = models.NFCTag(student_id=student.id, tag_uid=tag_uid, is_active=True)
    db_session.add(nfc)

    db_session.commit()
    db_session.refresh(student)
    db_session.refresh(wallet)
    db_session.refresh(nfc)
    return student, wallet, nfc


@pytest.fixture()
def student_with_wallet(db_session, school, parent_user):
    return make_student_with_wallet(db_session, school, parent_user)


@pytest.fixture()
def auth_headers(parent_user):
    token = create_access_token(
        user_id=parent_user.id, role=parent_user.role, phone=parent_user.phone,
    )
    return {"Authorization": f"Bearer {token}"}


def make_admin(db_session, school, *, phone):
    u = models.User(
        name="Test Admin", phone=phone, role="admin",
        pin_hash=hash_pin("1234"), school_id=school.id,
    )
    db_session.add(u)
    db_session.commit()
    db_session.refresh(u)
    return u


def headers_for(user):
    token = create_access_token(user_id=user.id, role=user.role, phone=user.phone)
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture()
def admin_user(db_session, school):
    return make_admin(db_session, school, phone="256700999001")


@pytest.fixture()
def admin_headers(admin_user):
    return headers_for(admin_user)


# ── Yo webhook signature verification ──────────────────
# A real Yo IPN carries an RSA signature we cannot forge in tests (per
# CLAUDE.md). Default to "signature valid" so tests can focus on the
# reference-routing/idempotency logic; individual tests can monkeypatch
# this back to False to exercise the rejection path.
@pytest.fixture(autouse=True)
def mock_yo_signature(monkeypatch):
    monkeypatch.setattr("app.routes.webhook.verify_yo_signature", lambda *a, **k: True)
