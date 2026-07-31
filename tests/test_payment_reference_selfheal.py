# ================================================
# tests/test_payment_reference_selfheal.py
# ------------------------------------------------
# Finding from the app/ audit: Transaction.reference got
# add_unique_constraint_if_missing() wired into create_tables() this
# session (see the webhook.py double-credit fix), but Payment.reference
# never did — even though it's already declared unique=True in
# models.py, and payments.py's nfc_payment()/sync_offline_payments()
# both depend on that constraint as their second line of defense (the
# wallet row lock is the first) against a duplicate request racing past
# the idempotency check.
#
# models.py's own comment on Payment already names this exact gap:
# create_all() doesn't retroactively ALTER an existing table's
# constraints, so an environment where the `payments` table was already
# created without this constraint needs the ALTER TABLE run by hand —
# which is exactly what add_unique_constraint_if_missing() automates,
# once something actually calls it for this table.
#
# Postgres-only (skips without TEST_DATABASE_URL): SQLite doesn't
# support ALTER TABLE ADD CONSTRAINT UNIQUE at all — add_unique_
# constraint_if_missing() would hit its except branch and just log a
# warning every time on SQLite, which wouldn't prove anything either
# way. Same reasoning as the with_for_update() tests in
# test_topup_status_race.py.
# ================================================

import os

import pytest
from sqlalchemy import create_engine, inspect, text

import app.database as db_module
from app.database import Base, add_unique_constraint_if_missing


@pytest.fixture()
def postgres_engine():
    url = os.environ.get("TEST_DATABASE_URL")
    if not url:
        pytest.skip(
            "Needs TEST_DATABASE_URL set to a throwaway Postgres db — SQLite "
            "doesn't support ALTER TABLE ADD CONSTRAINT UNIQUE at all, so it "
            "can't demonstrate this either way."
        )
    engine = create_engine(url)
    Base.metadata.drop_all(bind=engine)
    yield engine
    Base.metadata.drop_all(bind=engine)
    engine.dispose()


def _bare_payments_table(engine):
    """A `payments` table shaped like the model but WITHOUT the unique
    constraint — simulates the live table's actual current state."""
    with engine.begin() as conn:
        conn.execute(text("""
            CREATE TABLE payments (
                id SERIAL PRIMARY KEY,
                wallet_id INTEGER NOT NULL,
                amount FLOAT NOT NULL,
                status VARCHAR NOT NULL,
                reference VARCHAR,
                timestamp TIMESTAMP
            )
        """))


def test_add_unique_constraint_if_missing_adds_it_and_it_actually_enforces(postgres_engine, monkeypatch):
    _bare_payments_table(postgres_engine)
    assert inspect(postgres_engine).get_unique_constraints("payments") == []

    monkeypatch.setattr(db_module, "engine", postgres_engine)
    add_unique_constraint_if_missing("payments", "reference", "uq_payments_reference")

    constraints = inspect(postgres_engine).get_unique_constraints("payments")
    assert any(c["column_names"] == ["reference"] for c in constraints)

    with postgres_engine.begin() as conn:
        conn.execute(text(
            "INSERT INTO payments (wallet_id, amount, status, reference) "
            "VALUES (1, 100, 'completed', 'dup-ref')"
        ))
    with pytest.raises(Exception):
        with postgres_engine.begin() as conn:
            conn.execute(text(
                "INSERT INTO payments (wallet_id, amount, status, reference) "
                "VALUES (2, 200, 'completed', 'dup-ref')"
            ))


def test_add_unique_constraint_if_missing_is_a_safe_noop_when_already_present(postgres_engine, monkeypatch):
    Base.metadata.create_all(bind=postgres_engine)  # payments created fresh, WITH the constraint

    monkeypatch.setattr(db_module, "engine", postgres_engine)
    # Must not raise, and must not attempt a duplicate ALTER TABLE.
    add_unique_constraint_if_missing("payments", "reference", "uq_payments_reference")


def test_create_tables_self_heals_payments_reference_constraint(postgres_engine, monkeypatch):
    """
    The actual regression this fix is about: create_tables() must call
    add_unique_constraint_if_missing() for payments.reference, the same
    way it already does for transactions.reference. Fails red against
    today's create_tables() (no such call exists yet).
    """
    _bare_payments_table(postgres_engine)

    monkeypatch.setattr(db_module, "engine", postgres_engine)
    db_module.create_tables()

    constraints = inspect(postgres_engine).get_unique_constraints("payments")
    assert any(c["column_names"] == ["reference"] for c in constraints), (
        "create_tables() did not add the unique constraint on payments.reference"
    )
