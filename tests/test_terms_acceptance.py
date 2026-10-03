# ================================================
# tests/test_terms_acceptance.py
# ------------------------------------------------
# Terms and privacy acceptance, recorded per user.
#
#   GET  /auth/terms      public: current version, summary, full text
#   POST /auth/register   refuses unless the CURRENT version is accepted
#   POST /auth/login      a parent whose accepted version is not the
#                         current one gets no token until they accept
#
# What is recorded on User: WHICH version (terms_version) and WHEN
# (terms_accepted_at). A boolean would not say who agreed to what once
# the policy changes.
# ================================================

from datetime import datetime, timedelta

from app import models
from app.auth import hash_pin
from tests.conftest import make_admin, make_merchant_user

PHONE = "256700123456"
PIN = "4321"


def _current(client):
    res = client.get("/auth/terms")
    assert res.status_code == 200, res.text
    return res.json()["version"]


def _register(client, **overrides):
    body = {"name": "New Parent", "phone": PHONE, "pin": PIN}
    body.update(overrides)
    return client.post("/auth/register", json=body)


def _login(client, phone=PHONE, pin=PIN, **extra):
    return client.post("/auth/login", json={"phone": phone, "pin": pin, **extra})


def _parent(db_session, *, terms_version=None, accepted_at=None, phone=PHONE):
    u = models.User(
        name="Existing Parent", phone=phone, role="parent", pin_hash=hash_pin(PIN),
        terms_version=terms_version, terms_accepted_at=accepted_at,
    )
    db_session.add(u)
    db_session.commit()
    db_session.refresh(u)
    return u


def _user(db_session, phone=PHONE):
    db_session.expire_all()
    return db_session.query(models.User).filter_by(phone=phone).first()


# ── The terms themselves ─────────────────────────────
def test_terms_are_public_and_carry_a_version_summary_and_full_text(client):
    res = client.get("/auth/terms")
    assert res.status_code == 200
    body = res.json()
    assert isinstance(body["version"], str) and body["version"]
    assert body["summary"], "the acceptance screen needs a readable summary"
    for point in body["summary"]:
        assert point["title"] and point["body"]
    titles = [d["title"] for d in body["documents"]]
    assert "Terms of Use" in titles and "Privacy Policy" in titles
    for doc in body["documents"]:
        assert doc["sections"]


# ── Signup: accept BEFORE the account exists ─────────
def test_register_without_acceptance_creates_no_account(client, db_session):
    res = _register(client)
    assert res.status_code == 400
    assert "token" not in res.json()
    assert _user(db_session) is None


def test_register_with_an_old_or_unknown_version_creates_no_account(client, db_session):
    for bad in ("1999-01-01", "", "yes", "true"):
        res = _register(client, terms_version=bad)
        assert res.status_code == 400, bad
    assert _user(db_session) is None


def test_register_records_which_version_and_when(client, db_session):
    version = _current(client)
    before = datetime.utcnow()
    res = _register(client, terms_version=version)
    assert res.status_code == 200, res.text
    assert res.json()["token"]

    user = _user(db_session)
    assert user.terms_version == version
    assert before - timedelta(seconds=1) <= user.terms_accepted_at <= datetime.utcnow()

    # And they are not asked again at their next login.
    again = _login(client)
    assert again.status_code == 200
    assert again.json()["token"]


# ── Login: prompt again when the accepted version is not current ──
def test_parent_who_never_accepted_gets_no_token_until_they_accept(client, db_session):
    _parent(db_session)
    version = _current(client)

    res = _login(client)
    assert res.status_code == 403
    body = res.json()
    assert body["code"] == "terms_required"
    assert body["terms_version"] == version
    assert isinstance(body["detail"], str)
    assert "token" not in body
    assert _user(db_session).terms_version is None

    accepted = _login(client, accept_terms_version=version)
    assert accepted.status_code == 200
    assert accepted.json()["token"]
    user = _user(db_session)
    assert user.terms_version == version
    assert user.terms_accepted_at is not None


def test_a_correct_pin_awaiting_acceptance_is_not_a_failed_attempt(client, db_session):
    _parent(db_session)
    for _ in range(7):
        assert _login(client).status_code == 403
    user = _user(db_session)
    assert user.failed_login_attempts == 0
    assert user.locked_until is None


def test_when_the_version_changes_the_parent_is_prompted_and_the_new_acceptance_recorded(
    client, db_session, monkeypatch,
):
    old_version = _current(client)
    long_ago = datetime.utcnow() - timedelta(days=90)
    _parent(db_session, terms_version=old_version, accepted_at=long_ago)
    assert _login(client).status_code == 200

    from app import terms
    monkeypatch.setattr(terms, "CURRENT_TERMS_VERSION", "2099-01-01")

    res = _login(client)
    assert res.status_code == 403
    assert res.json()["terms_version"] == "2099-01-01"
    # Nothing changes until they actually accept.
    user = _user(db_session)
    assert user.terms_version == old_version
    assert user.terms_accepted_at == long_ago

    # Accepting the version they saw before the change is not enough.
    assert _login(client, accept_terms_version=old_version).status_code == 403

    assert _login(client, accept_terms_version="2099-01-01").status_code == 200
    user = _user(db_session)
    assert user.terms_version == "2099-01-01"
    assert user.terms_accepted_at > long_ago


def test_logging_in_with_the_current_version_does_not_move_the_timestamp(client, db_session):
    version = _current(client)
    when = datetime.utcnow() - timedelta(days=3)
    _parent(db_session, terms_version=version, accepted_at=when)

    assert _login(client, accept_terms_version=version).status_code == 200
    assert _user(db_session).terms_accepted_at == when


def test_a_wrong_pin_records_no_acceptance(client, db_session):
    _parent(db_session)
    version = _current(client)
    res = _login(client, pin="0000", accept_terms_version=version)
    assert res.status_code == 401
    user = _user(db_session)
    assert user.terms_version is None
    assert user.terms_accepted_at is None


def test_unknown_number_gets_the_same_answer_as_before(client):
    # The terms gate must not reveal whether a number has an account.
    res = _login(client, phone="256700000999", accept_terms_version=_current(client))
    assert res.status_code == 401


def test_school_staff_are_not_gated(client, db_session, school):
    # The till and card pages sign in with /auth/login and have no
    # acceptance screen. This requirement is for parents.
    make_admin(db_session, school, phone="256700999031")
    make_merchant_user(db_session, school, phone="256700999032")
    for phone in ("256700999031", "256700999032"):
        res = _login(client, phone=phone, pin="1234")
        assert res.status_code == 200, res.text
        assert res.json()["token"]


# ── The migration ────────────────────────────────────
def test_migration_adds_exactly_the_columns_the_model_declares():
    import pathlib
    sql = pathlib.Path("migrations/2026_10_02_add_terms_acceptance.sql").read_text()
    for column in ("terms_version", "terms_accepted_at"):
        assert hasattr(models.User, column)
        assert column in sql
    # Only the statements that run, not the commented undo notes.
    lowered = "\n".join(
        line for line in sql.lower().splitlines() if not line.lstrip().startswith("--")
    )
    assert "alter table users" in lowered
    for destructive in ("drop ", "delete ", "update ", "truncate "):
        assert destructive not in lowered


def test_served_terms_have_no_unfilled_gaps(client):
    # _ask() marks wording only the operator can supply. Parents must
    # never be asked to accept text that still says "[TO CONFIRM".
    assert "[TO CONFIRM" not in client.get("/auth/terms").text
    assert "[PLACEHOLDER" not in client.get("/auth/terms").text
