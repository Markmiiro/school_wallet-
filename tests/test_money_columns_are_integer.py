# ================================================
# tests/test_money_columns_are_integer.py
# ------------------------------------------------
# Finding from the app/ audit: Wallet.balance, Transaction.amount, and
# Payment.amount were Float, not Integer/Decimal — UGX has no subunit,
# and storing money as a binary float is a structural footgun for any
# future code that ever divides an amount (a refund split, a discount
# percentage, proration) and stores the fractional result, even though
# nothing in today's codebase currently does that (every request
# schema already types amounts as `int`, and webhook.py explicitly
# casts int(float(amount_str)) before touching the DB — which is why
# the live-database audit run alongside this change is expected to
# come back clean rather than "prove" a bug already fired).
#
# What these tests actually verify:
#   1. The three columns are genuinely Integer at the SQLAlchemy/model
#      level (not just "equals an int-valued float", which Float
#      columns already satisfied and wouldn't have caught a schema
#      regression).
#   2. A realistic sequence of mixed credits/debits through the real
#      endpoints nets out to an exact integer with zero drift — this
#      is regression coverage for the migration, not a demonstration
#      that the old Float columns were already broken (integer-valued
#      float64 arithmetic is exact well within any realistic UGX
#      balance, so this specific sequence would have passed under the
#      old columns too — the fix closes the structural risk, not an
#      observed one).
# ================================================

from sqlalchemy import Integer

from app.models import Payment, Transaction, Wallet
from tests.conftest import make_student_with_wallet


def test_money_columns_are_integer_type():
    assert isinstance(Wallet.balance.type, Integer)
    assert isinstance(Transaction.amount.type, Integer)
    assert isinstance(Payment.amount.type, Integer)


def test_many_sequential_payments_net_out_to_exact_integer_balance(
    client, db_session, school, parent_user, merchant, auth_headers
):
    student, wallet, nfc = make_student_with_wallet(
        db_session, school, parent_user, balance=1_000_000, daily_limit=1_000_000,
    )

    amounts = [1, 3, 7, 11, 13, 17, 23, 29, 31, 37] * 5  # 50 odd/awkward amounts
    total_spent = 0
    for i, amount in enumerate(amounts):
        res = client.post(
            "/payments/nfc",
            params={
                "tag_uid": nfc.tag_uid,
                "merchant_id": merchant.id,
                "amount": amount,
                "request_id": f"drift-check-{i}",
                "description": "drift check",
            },
            headers=auth_headers,
        )
        assert res.status_code == 200
        total_spent += amount

    db_session.refresh(wallet)
    expected = 1_000_000 - total_spent
    assert wallet.balance == expected
    assert isinstance(wallet.balance, int)
