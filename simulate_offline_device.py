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
            description TEXT,
            timestamp   TEXT,
            synced      INTEGER DEFAULT 0
        )
    """)
    conn.commit()
    conn.close()
    print("✅ Local device storage ready")


def save_offline_payment(tag_uid: str, amount: int, description: str):
    """Save a payment locally when no internet."""
    conn = sqlite3.connect(LOCAL_DB)
    conn.execute(
        "INSERT INTO offline_payments (tag_uid, amount, description, timestamp) VALUES (?, ?, ?, ?)",
        (tag_uid, amount, description, datetime.utcnow().isoformat())
    )
    conn.commit()
    conn.close()
    print(f"💾 Saved offline: {tag_uid} UGX {amount:,}")


def get_unsynced_payments():
    """Get all payments not yet synced to server."""
    conn = sqlite3.connect(LOCAL_DB)
    rows = conn.execute(
        "SELECT id, tag_uid, amount, description, timestamp FROM offline_payments WHERE synced = 0"
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
            "description": row[3],
            "timestamp":   row[4],
        }
        for row in unsynced
    ]

    try:
        response = requests.post(
            f"{SERVER_URL}/payments/sync",
            params={
                "merchant_id": MERCHANT_ID,
                "device_id":   DEVICE_ID,
            },
            json=payments,
            timeout=10,
        )

        result = response.json()
        print(f"✅ Server response: {result['message']}")
        print(f"   Processed: {result['processed']}")
        print(f"   Failed:    {result['failed']}")

        # Mark successfully processed ones as synced
        if result["processed"] > 0:
            synced_tags = [p["tag_uid"] for p in result["details"]["processed"]]
            ids_to_mark = [
                row[0] for row in unsynced
                if row[1] in synced_tags
            ]
            mark_as_synced(ids_to_mark)
            print(f"✅ {len(ids_to_mark)} payments marked as synced locally")

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