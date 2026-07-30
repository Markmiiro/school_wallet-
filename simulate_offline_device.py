# ================================================
# simulate_offline_device.py
# ------------------------------------------------
# Simulates what a tuck shop NFC reader device does:
# 1. Stores payments locally when offline
# 2. Syncs to server when internet returns
#
# Run this to test the full offline flow:
#   python simulate_offline_device.py
# ================================================

import requests
import json
import sqlite3
import uuid
from datetime import datetime

SERVER_URL  = "http://127.0.0.1:8000"
DEVICE_ID   = "tuckshop-device-001"
MERCHANT_ID = 1

# /payments/sync requires a signed-in staff account, same as the real
# tuck-shop device page. This is a local dev/test login — never real
# credentials, and never point SERVER_URL at anything but a local or
# throwaway server.
STAFF_PHONE = "256700000001"
STAFF_PIN   = "1234"

# Local database — simulates device storage
LOCAL_DB = "offline_payments.db"


def setup_local_db():
    """Create local storage for offline payments."""
    conn = sqlite3.connect(LOCAL_DB)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS offline_payments (
            id          INTEGER PRIMARY KEY,
            tag_uid     TEXT NOT NULL,
            amount      INTEGER NOT NULL,
            request_id  TEXT NOT NULL,
            description TEXT,
            timestamp   TEXT,
            synced      INTEGER DEFAULT 0
        )
    """)
    conn.commit()
    conn.close()
    print("✅ Local device storage ready")


def save_offline_payment(tag_uid: str, amount: int, description: str):
    """Save a payment locally when no internet.

    request_id is generated once, here, at the moment of the tap — not
    regenerated at sync time. That's what makes resyncing this exact row
    safe to repeat: the server recognizes the same request_id as "already
    processed" instead of charging it again (see app/routes/payments.py's
    sync_offline_payments()).
    """
    request_id = str(uuid.uuid4())
    conn = sqlite3.connect(LOCAL_DB)
    conn.execute(
        "INSERT INTO offline_payments (tag_uid, amount, request_id, description, timestamp) "
        "VALUES (?, ?, ?, ?, ?)",
        (tag_uid, amount, request_id, description, datetime.utcnow().isoformat())
    )
    conn.commit()
    conn.close()
    print(f"💾 Saved offline: {tag_uid} UGX {amount:,} (request_id {request_id[:8]}…)")


def get_unsynced_payments():
    """Get all payments not yet synced to server."""
    conn = sqlite3.connect(LOCAL_DB)
    rows = conn.execute(
        "SELECT id, tag_uid, amount, request_id, description, timestamp "
        "FROM offline_payments WHERE synced = 0"
    ).fetchall()
    conn.close()
    return rows


def mark_as_synced(payment_ids: list):
    """Mark payments as synced after successful upload."""
    conn = sqlite3.connect(LOCAL_DB)
    for pid in payment_ids:
        conn.execute(
            "UPDATE offline_payments SET synced = 1 WHERE id = ?", (pid,)
        )
    conn.commit()
    conn.close()


def login() -> str:
    """Get a bearer token the same way the real tuck-shop page does."""
    res = requests.post(
        f"{SERVER_URL}/auth/login",
        json={"phone": STAFF_PHONE, "pin": STAFF_PIN},
        timeout=10,
    )
    res.raise_for_status()
    token = res.json().get("token")
    if not token:
        raise RuntimeError("Login succeeded but no token was returned")
    return token


def sync_to_server():
    """Send all unsynced payments to the server."""
    unsynced = get_unsynced_payments()

    if not unsynced:
        print("✅ Nothing to sync — all payments up to date")
        return

    print(f"\n🔄 Syncing {len(unsynced)} offline payments to server...")

    # Build the payload
    payments = [
        {
            "tag_uid":     row[1],
            "amount":      row[2],
            "request_id":  row[3],
            "description": row[4],
            "timestamp":   row[5],
        }
        for row in unsynced
    ]

    try:
        token = login()
    except Exception as e:
        print(f"❌ Could not sign in — still offline or bad credentials: {e}")
        return

    try:
        response = requests.post(
            f"{SERVER_URL}/payments/sync",
            params={
                "merchant_id": MERCHANT_ID,
                "device_id":   DEVICE_ID,
            },
            json=payments,
            headers={"Authorization": f"Bearer {token}"},
            timeout=10,
        )

        result = response.json()
        print(f"✅ Server response: {result['message']}")
        print(f"   Processed: {result['processed']}")
        print(f"   Failed:    {result['failed']}")

        # Mark synced rows by request_id, not tag_uid. tag_uid isn't unique
        # within a batch (the same student's card can appear twice —
        # two separate purchases), so matching on it would wrongly mark
        # every local row for that card as synced off a single response
        # entry. request_id is unique per attempt, so it's the only safe
        # key to reconcile with — the server echoes it back on every
        # processed AND failed entry for exactly this reason.
        settled_ids = {
            p.get("request_id") for p in result["details"]["processed"]
        } | {
            f.get("request_id") for f in result["details"]["failed"]
        }
        ids_to_mark = [row[0] for row in unsynced if row[3] in settled_ids]
        if ids_to_mark:
            mark_as_synced(ids_to_mark)
            print(f"✅ {len(ids_to_mark)} payment(s) settled locally "
                  f"({result['processed']} processed, "
                  f"{len(ids_to_mark) - result['processed']} permanently failed)")

        still_pending = len(unsynced) - len(ids_to_mark)
        if still_pending:
            print(f"⏳ {still_pending} payment(s) got no response from the server "
                  f"and remain queued for the next sync attempt")

    except requests.exceptions.ConnectionError:
        print("❌ Cannot reach server — still offline, will retry later")
    except Exception as e:
        print(f"❌ Sync error: {e}")


# ── RUN THE SIMULATION ───────────────────────────
if __name__ == "__main__":
    print("\n" + "="*50)
    print("🏪 TUCK SHOP OFFLINE DEVICE SIMULATOR")
    print("="*50)

    setup_local_db()

    print("\n📴 SIMULATING OFFLINE MODE...")
    print("Saving 3 payments locally (no internet)")

    # Simulate 3 students tapping while offline
    # Use the NFC tag UID you assigned to Amara earlier
    # If you haven't assigned one yet use: PUT /students/1/assign-nfc?tag_uid=ABC123XY
    save_offline_payment("ABC123XY", 2000, "Lunch - rice and beans")
    save_offline_payment("ABC123XY", 500,  "Snack - biscuits")
    save_offline_payment("ABC123XY", 1000, "Drink - juice")

    print("\n📶 SIMULATING INTERNET RETURNING...")
    sync_to_server()

    print("\n" + "="*50)
