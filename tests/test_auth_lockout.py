# ================================================
# tests/test_auth_lockout.py
# ------------------------------------------------
# Money-critical: auth rate-limiting.
#   POST /auth/login  (app/routes/auth.py)
#
# app/routes/auth.py is NOT on CLAUDE.md's frozen list (only
# app/auth.py — the utilities it imports FROM — is money-adjacent
# read-only). Bugs found here can be discussed for a fix, but I'm
# still reporting rather than silently patching per the project's
# general caution around auth.
#
# parent_user fixture (conftest.py) is created with PIN "1234".
# ================================================

from datetime import datetime, timedelta

import pytest

from app.routes.auth import MAX_FAILED_ATTEMPTS, LOCKOUT_MINUTES

PIN = "1234"
WRONG_PIN = "0000"


def login(client, phone, pin):
    return client.post("/auth/login", json={"phone": phone, "pin": pin})


# ── Happy path ──────────────────────────────────────
def test_correct_pin_logs_in_and_returns_token(client, parent_user):
    r = login(client, parent_user.phone, PIN)
    assert r.status_code == 200
    body = r.json()
    assert body["token"]
    assert body["user"]["phone"] == parent_user.phone


def test_unknown_phone_returns_401(client):
    r = login(client, "256799999999", PIN)
    assert r.status_code == 401


# ── Lockout progression ──────────────────────────────
def test_wrong_pin_increments_failed_attempts(client, db_session, parent_user):
    r = login(client, parent_user.phone, WRONG_PIN)
    assert r.status_code == 401
    assert "4 attempt" in r.json()["detail"]

    db_session.refresh(parent_user)
    assert parent_user.failed_login_attempts == 1
    assert parent_user.locked_until is None


def test_fifth_wrong_pin_locks_account(client, db_session, parent_user):
    for _ in range(MAX_FAILED_ATTEMPTS - 1):
        r = login(client, parent_user.phone, WRONG_PIN)
        assert r.status_code == 401

    locking_attempt = login(client, parent_user.phone, WRONG_PIN)
    assert locking_attempt.status_code == 429
    assert "locked" in locking_attempt.json()["detail"].lower()

    db_session.refresh(parent_user)
    assert parent_user.failed_login_attempts == MAX_FAILED_ATTEMPTS
    assert parent_user.locked_until is not None
    assert parent_user.locked_until > datetime.utcnow()


def test_locked_account_rejects_even_correct_pin(client, db_session, parent_user):
    for _ in range(MAX_FAILED_ATTEMPTS):
        login(client, parent_user.phone, WRONG_PIN)

    r = login(client, parent_user.phone, PIN)
    assert r.status_code == 429, "a locked account must reject the correct PIN too, until the lockout expires"


def test_correct_pin_after_some_failures_resets_counter(client, db_session, parent_user):
    login(client, parent_user.phone, WRONG_PIN)
    login(client, parent_user.phone, WRONG_PIN)
    db_session.refresh(parent_user)
    assert parent_user.failed_login_attempts == 2

    r = login(client, parent_user.phone, PIN)
    assert r.status_code == 200

    db_session.refresh(parent_user)
    assert parent_user.failed_login_attempts == 0
    assert parent_user.locked_until is None


def test_correct_pin_after_lockout_expires_succeeds_and_resets(client, db_session, parent_user):
    parent_user.failed_login_attempts = MAX_FAILED_ATTEMPTS
    parent_user.locked_until = datetime.utcnow() - timedelta(seconds=1)  # already expired
    db_session.commit()

    r = login(client, parent_user.phone, PIN)
    assert r.status_code == 200

    db_session.refresh(parent_user)
    assert parent_user.failed_login_attempts == 0
    assert parent_user.locked_until is None


# ── Phone-number enumeration ──────────────────────────
# FIXED: the "unknown phone" branch now returns the exact wording a
# fresh account's first wrong-PIN attempt would return, so a single
# probe can't tell "not registered" from "registered, wrong PIN" by
# response text (both are also already 401, so status code doesn't
# leak it either).
#
# This isn't a perfect defense — if an attacker first drives up
# failed_login_attempts on a real, known-to-exist number, that
# number's count diverges from this fixed message. Closing that
# residual gap would need a bigger change (e.g. rate-limiting by
# caller regardless of phone validity), out of scope here.
def test_unknown_phone_and_wrong_pin_are_indistinguishable(client, parent_user):
    unknown = login(client, "256799999999", PIN)
    wrong_pin = login(client, parent_user.phone, WRONG_PIN)

    assert unknown.status_code == wrong_pin.status_code == 401
    assert unknown.json()["detail"] == wrong_pin.json()["detail"], (
        f"responses are distinguishable: unknown-phone={unknown.json()['detail']!r} "
        f"vs wrong-pin={wrong_pin.json()['detail']!r}"
    )
