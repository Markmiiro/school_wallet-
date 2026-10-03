# ================================================
# tests/test_wallet_history_purchases.py
# ------------------------------------------------
# What the parent app needs from GET /wallets/{id}/history to show a
# purchase. The till (app/routes/tuckshop.py) sends every payment with
# the description "Tuck shop purchase", so the tuck shop's name must come
# from the transaction's merchant, not from the description.
# ================================================

from app import models


def _till_pay(client, nfc, merchant, staff_headers, amount, request_id):
    # Exactly what the till page sends.
    return client.post("/payments/nfc", params={
        "tag_uid": nfc.tag_uid, "merchant_id": merchant.id, "amount": amount,
        "description": "Tuck shop purchase", "request_id": request_id,
    }, headers=staff_headers)


def test_a_till_purchase_carries_the_tuck_shops_name(
    client, student_with_wallet, merchant, auth_headers, staff_headers,
):
    student, _, nfc = student_with_wallet
    assert _till_pay(client, nfc, merchant, staff_headers, 1500, "req-0001").status_code == 200

    tx = client.get(f"/wallets/{student.id}/history", headers=auth_headers).json()["transactions"][0]
    assert tx["type"] == "payment"
    assert tx["direction"] == "OUT"
    assert tx["amount"] == 1500
    assert tx["description"] == "Tuck shop purchase"
    assert tx["merchant"] == merchant.name


def test_a_top_up_has_no_merchant(client, db_session, student_with_wallet, auth_headers):
    student, wallet, _ = student_with_wallet
    db_session.add(models.Transaction(
        wallet_id=wallet.id, amount=5000, type="topup", status="completed",
        reference="topup-ref-1", description="Top-up for Test Student",
    ))
    db_session.commit()
    tx = client.get(f"/wallets/{student.id}/history", headers=auth_headers).json()["transactions"][0]
    assert tx["type"] == "topup" and tx["merchant"] is None


def test_totals_cover_every_transaction_not_only_the_page_returned(
    client, student_with_wallet, merchant, auth_headers, staff_headers,
):
    student, _, nfc = student_with_wallet
    for i in range(3):
        assert _till_pay(client, nfc, merchant, staff_headers, 1000, f"req-10{i}").status_code == 200

    body = client.get(f"/wallets/{student.id}/history", params={"limit": 2},
                      headers=auth_headers).json()
    assert len(body["transactions"]) == 2
    assert body["summary"]["total_spent"] == 3000
    assert body["summary"]["number_of_transactions"] == 3
