# ================================================
# tests/test_topup_status_race.py
# ------------------------------------------------
# Money-critical: app/routes/topup.py's check_topup_status() (GET
# /topup/{reference_id}) — the same check-then-act double-credit class
# of bug already fixed in app/routes/webhook.py, but easier to trigger:
#
#     if latest_status == "SUCCEEDED" and txn.status != "completed":
#         wallet.balance += txn.amount
#         txn.status = "completed"
#         db.commit()
#
# No with_for_update() lock, and — unlike webhook.py's yo_uganda_ipn(),
# which has no `await` between its check and its commit — this endpoint
# has a real `await verify_transaction(reference_id)` sitting right
# between reading txn.status and crediting the wallet. That means two
# concurrent requests can genuinely interleave on a single event loop:
# both load their own (still "pending") Transaction object, both await
# Yo's status check, and both come back to a check that's still true in
# their own copy — no multi-process/Postgres setup needed to observe
# this, unlike the webhook.py cross-process race.
#
# Transaction.reference's unique constraint (added earlier this session)
# does NOT help here — this is an UPDATE to an existing row, not an
# INSERT, so there's no second row for the constraint to reject.
#
# NOTE on why this test doesn't skip on SQLite (unlike the equivalent
# webhook.py concurrency tests): with_for_update() silently no-ops on
# SQLite, so the pass you get here isn't proof the lock works — it's
# StaticPool's single shared connection incidentally serializing the two
# threads' actual statement execution closely enough that the second
# read lands after the first commit. That's a coincidence of this test
# environment, not a guarantee. The real proof is the Postgres run:
#   TEST_DATABASE_URL=postgresql:///<throwaway_db> ./venv/bin/python -m pytest tests/test_topup_status_race.py
# which failed (double credit) before populate_existing()+with_for_update()
# were added, and passes now that they are.
# ================================================

import asyncio
import concurrent.futures

from app.models import Transaction
from tests.conftest import make_student_with_wallet


async def _fake_verify_succeeded(reference_id):
    # The delay is what makes the race deterministic in a test instead of
    # a timing coin-flip: it guarantees both concurrent requests reach
    # this await, and are both still holding their own stale in-memory
    # txn.status == "pending", before either one resumes and commits.
    await asyncio.sleep(0.05)
    return {"Status": "OK", "TransactionStatus": "SUCCEEDED"}


def test_concurrent_status_poll_double_credits_wallet(
    client, db_session, school, parent_user, auth_headers, monkeypatch
):
    student, wallet, nfc = make_student_with_wallet(db_session, school, parent_user, balance=1000)

    txn = Transaction(
        wallet_id=wallet.id,
        amount=5000,
        type="topup",
        status="pending",
        reference="test-topup-ref-race",
    )
    db_session.add(txn)
    db_session.commit()

    monkeypatch.setattr("app.routes.topup.verify_transaction", _fake_verify_succeeded)

    def fire():
        return client.get("/topup/test-topup-ref-race", headers=auth_headers)

    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: fire(), range(2)))

    assert all(r.status_code == 200 for r in results)

    db_session.refresh(wallet)
    assert wallet.balance == 6000, (
        f"two concurrent polls of the same pending top-up must not double-credit "
        f"the wallet — got {wallet.balance}, expected 6000 (1000 + 5000 once, "
        f"not twice)"
    )

    completed_count = (
        db_session.query(Transaction)
        .filter_by(reference="test-topup-ref-race", status="completed")
        .count()
    )
    assert completed_count == 1
