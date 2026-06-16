"""
================================================
test_school_wallet.py
------------------------------------------------
Full automated test for School Wallet Uganda.
Tests the complete payment flow end to end.

HOW TO RUN:
  pip install httpx
  python test_school_wallet.py
================================================
"""

import httpx
import asyncio
import json
import sys
import uuid
import xml.etree.ElementTree as ET
from datetime import datetime

BASE_URL    = "https://web-production-454a5.up.railway.app"
YO_API_URL  = "https://sandbox.yo.co.ug/services/yopaymentsdev/"
YO_USERNAME = "90000125126"
YO_PASSWORD = "oT0p-LbRm-yq6d-Xse2-AUwO-pUpn-3tJM-G2TO"
ADMIN_PHONE = "256700000001"
ADMIN_PIN   = "1234"

GREEN  = "\033[92m"
RED    = "\033[91m"
YELLOW = "\033[93m"
BLUE   = "\033[94m"
BOLD   = "\033[1m"
RESET  = "\033[0m"

results = []

def ok(label, detail=""):
    results.append(("PASS", label))
    print(f"  {GREEN}OK{RESET}  {label}")
    if detail:
        print(f"       {YELLOW}{detail}{RESET}")

def fail(label, detail=""):
    results.append(("FAIL", label))
    print(f"  {RED}FAIL{RESET}  {label}")
    if detail:
        print(f"       {RED}{detail}{RESET}")

def info(msg):
    print(f"  {YELLOW}>>  {msg}{RESET}")

def header(title):
    print(f"\n{BOLD}{BLUE}--- {title} ---{RESET}")


async def step_login(client):
    header("STEP 1 - Login as admin")
    try:
        r = await client.post(
            f"{BASE_URL}/auth/login",
            json={"phone": ADMIN_PHONE, "pin": ADMIN_PIN},
            timeout=15
        )
        if r.status_code == 200:
            token = r.json().get("token", "")

            if token:
                ok("Admin login successful", f"Phone: {ADMIN_PHONE}")
                return token
            else:
                fail("Login response missing access_token", str(r.json()))
        else:
            fail(f"Login failed HTTP {r.status_code}",
                 r.json().get("detail", r.text[:100]))
    except Exception as e:
        fail("Login request failed", str(e))
    return None


async def step_create_school(client, token):
    header("STEP 2 - Create test school")
    school_name = f"Test School {uuid.uuid4().hex[:6].upper()}"
    try:
        r = await client.post(
            f"{BASE_URL}/schools/",
            params={"name": school_name, "location": "Kampala Uganda"},
            headers={"Authorization": f"Bearer {token}"},
            timeout=15
        )
        if r.status_code in (405, 422):
            r = await client.post(
                f"{BASE_URL}/schools/",
                json={"name": school_name, "location": "Kampala Uganda"},
                headers={"Authorization": f"Bearer {token}"},
                timeout=15
            )
        if r.status_code == 200:
            data = r.json()
            school_id = (
                data.get("id") or
                data.get("school", {}).get("id") or
                data.get("school_id")
            )
            if school_id:
                ok(f"School created: {school_name}", f"ID: {school_id}")
                return school_id
            else:
                fail("No school ID in response", str(data)[:150])
        else:
            fail(f"School creation failed HTTP {r.status_code}", r.text[:150])
    except Exception as e:
        fail("School creation error", str(e))
    return None


async def step_create_parent(client, token):
    header("STEP 3 - Create test parent")
    parent_phone = "256" + uuid.uuid4().hex[:9]
    parent_phone = parent_phone[:12]
    try:
        r = await client.post(
            f"{BASE_URL}/users/",
            params={"name": "Test Parent", "phone": parent_phone, "role": "parent"},
            headers={"Authorization": f"Bearer {token}"},
            timeout=15
        )
        if r.status_code in (405, 422):
            r = await client.post(
                f"{BASE_URL}/users/",
                json={"name": "Test Parent", "phone": parent_phone, "role": "parent"},
                headers={"Authorization": f"Bearer {token}"},
                timeout=15
            )
        if r.status_code == 200:
            data = r.json()
            parent_id = data.get("id") or data.get("user", {}).get("id")
            if parent_id:
                ok(f"Parent created", f"ID: {parent_id}  Phone: {parent_phone}")
                return parent_id, parent_phone
            else:
                fail("No parent ID in response", str(data)[:150])
        else:
            fail(f"Parent creation failed HTTP {r.status_code}", r.text[:150])
    except Exception as e:
        fail("Parent creation error", str(e))
    return None, None


async def step_create_student(client, token, school_id, parent_id):
    header("STEP 4 - Create test student (auto-creates wallet)")
    student_name = f"Test Student {uuid.uuid4().hex[:4].upper()}"
    try:
        r = await client.post(
            f"{BASE_URL}/students/",
            params={"name": student_name, "school_id": school_id, "parent_id": parent_id},
            headers={"Authorization": f"Bearer {token}"},
            timeout=15
        )
        if r.status_code in (405, 422):
            r = await client.post(
                f"{BASE_URL}/students/",
                json={"name": student_name, "school_id": school_id, "parent_id": parent_id},
                headers={"Authorization": f"Bearer {token}"},
                timeout=15
            )
        if r.status_code == 200:
            data       = r.json()
            student    = data.get("student", {})
            wallet     = data.get("wallet", {})
            student_id = student.get("id")
            wallet_id  = wallet.get("id")
            if student_id:
                ok(f"Student created: {student_name}",
                   f"Student ID: {student_id}  Wallet ID: {wallet_id}")
                return student_id, wallet_id
            else:
                fail("No student ID in response", str(data)[:150])
        else:
            fail(f"Student creation failed HTTP {r.status_code}", r.text[:150])
    except Exception as e:
        fail("Student creation error", str(e))
    return None, None


async def step_get_wallet(client, token, student_id):
    header("STEP 5 - Get student wallet")
    try:
        r = await client.get(
            f"{BASE_URL}/wallets/wallets/{student_id}",
            headers={"Authorization": f"Bearer {token}"},
            timeout=15
        )
        if r.status_code == 200:
            data      = r.json()
            balance   = data.get("balance", data.get("balance_ugx", 0))
            wallet_id = data.get("id", data.get("wallet_id"))
            ok(f"Wallet found", f"Balance: UGX {balance:,.0f}  Wallet ID: {wallet_id}")
            return wallet_id, balance
        else:
            fail(f"Wallet fetch failed HTTP {r.status_code}", r.text[:150])
    except Exception as e:
        fail("Wallet fetch error", str(e))
    return None, 0


async def step_initiate_topup(client, token, wallet_id):
    header("STEP 6 - Initiate top-up via Yo Uganda")
    payload = {
        "wallet_id":    wallet_id,
        "amount":       5000,
        "phone_number": "256771234567",
        "network":      "MTN",
        "note":         "Automated test top-up"
    }
    try:
        r = await client.post(
            f"{BASE_URL}/topup/",
            json=payload,
            headers={"Authorization": f"Bearer {token}"},
            timeout=20
        )
        if r.status_code == 200:
            data   = r.json()
            ref    = data.get("reference_id", "")
            status = data.get("status", "")
            if ref and status == "pending":
                ok("Top-up initiated successfully",
                   f"Ref: {ref[:16]}...  Status: {status}")
                return ref
            else:
                fail("Top-up response unexpected", json.dumps(data)[:150])
        elif r.status_code == 422:
            fail("Validation error", str(r.json().get("detail", ""))[:150])
        elif r.status_code == 401:
            fail("Unauthorised — token may be expired")
        else:
            fail(f"Top-up failed HTTP {r.status_code}", r.text[:150])
    except Exception as e:
        fail("Top-up error", str(e))
    return None


async def step_fire_webhook(client, reference_id):
    header("STEP 7 - Simulate Yo Uganda IPN webhook")
    if not reference_id:
        fail("Skipping — no reference ID from Step 6")
        return False
    webhook_data = {
        "ExternalReference":   reference_id,
        "TransactionStatus":   "SUCCEEDED",
        "Amount":              "5000",
        "TransactionReference": f"YO-{uuid.uuid4().hex[:8].upper()}",
    }
    try:
        r = await client.post(
            f"{BASE_URL}/webhook/yo",
            data=webhook_data,
            timeout=20
        )
        if r.status_code == 200:
            msg = r.json().get("message", "")
            if "credited" in msg.lower():
                ok("Webhook processed — wallet credited", f"Response: {msg}")
                return True
            elif "already processed" in msg.lower():
                ok("Already processed (idempotency)", msg)
                return True
            else:
                fail("Webhook response unexpected", msg)
        else:
            fail(f"Webhook failed HTTP {r.status_code}", r.text[:150])
    except Exception as e:
        fail("Webhook error", str(e))
    return False


async def step_confirm_balance(client, token, student_id, balance_before):
    header("STEP 8 - Confirm wallet balance increased")
    await asyncio.sleep(1)
    try:
        r = await client.get(
            f"{BASE_URL}/wallets/wallets/{student_id}",
            headers={"Authorization": f"Bearer {token}"},
            timeout=15
        )
        if r.status_code == 200:
            data          = r.json()
            balance_after = data.get("balance", data.get("balance_ugx", 0))
            info(f"Balance before: UGX {balance_before:,.0f}")
            info(f"Balance after:  UGX {balance_after:,.0f}")
            if balance_after > balance_before:
                ok(f"Balance increased by UGX {balance_after - balance_before:,.0f}")
            else:
                fail("Balance did not increase")
        else:
            fail(f"Balance check failed HTTP {r.status_code}", r.text[:100])
    except Exception as e:
        fail("Balance check error", str(e))


async def step_idempotency(client, reference_id):
    header("STEP 9 - Idempotency (same webhook twice)")
    if not reference_id:
        fail("Skipping — no reference ID")
        return
    try:
        r = await client.post(
            f"{BASE_URL}/webhook/yo",
            data={"ExternalReference": reference_id, "TransactionStatus": "SUCCEEDED", "Amount": "5000"},
            timeout=20
        )
        if r.status_code == 200:
            msg = r.json().get("message", "")
            if "already processed" in msg.lower():
                ok("Idempotency works — duplicate webhook rejected", msg)
            else:
                fail("Idempotency failed — processed twice!", msg)
        else:
            fail(f"Idempotency test failed HTTP {r.status_code}")
    except Exception as e:
        fail("Idempotency error", str(e))


async def step_yo_sandbox(client):
    header("STEP 10 - Test Yo Uganda sandbox API")
    test_ref = f"SWTEST-{uuid.uuid4().hex[:12].upper()}"
    xml_req = f"""<?xml version="1.0" encoding="UTF-8"?>
<AutoCreate>
  <Request>
    <APIUsername>{YO_USERNAME}</APIUsername>
    <APIPassword>{YO_PASSWORD}</APIPassword>
    <Method>acdepositfunds</Method>
    <Amount>1000</Amount>
    <Account>256771234567</Account>
    <Currency>UGX</Currency>
    <Narrative>School Wallet test</Narrative>
    <ExternalReference>{test_ref}</ExternalReference>
    <NonBlocking>TRUE</NonBlocking>
    <InstantPaymentNotificationURL>{BASE_URL}/webhook/yo</InstantPaymentNotificationURL>
  </Request>
</AutoCreate>""".strip()
    try:
        r = await client.post(
            YO_API_URL,
            content=xml_req,
            headers={"Content-Type": "application/xml"},
            timeout=30
        )
        info(f"Yo Uganda HTTP: {r.status_code}")
        info(f"Response: {r.text[:200]}")
        try:
            root   = ET.fromstring(r.text)
            result = {}
            for child in root.iter():
                if child.text and child.text.strip():
                    result[child.tag] = child.text.strip()
            status = result.get("Status", "UNKNOWN")
            msg    = result.get("StatusMessage", "")
            if status == "OK":
                ok("Yo Uganda sandbox accepted the request",
                   f"TxStatus: {result.get('TransactionStatus')}")
            else:
                fail(f"Yo Uganda status: {status}", msg)
        except ET.ParseError:
            fail("Could not parse Yo Uganda XML",
                 "Run this test on your own computer")
    except Exception as e:
        fail("Could not reach Yo Uganda", str(e))


async def main():
    print(f"\n{'='*55}")
    print(f"  SCHOOL WALLET — FULL INTEGRATION TEST")
    print(f"  {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"  {BASE_URL}")
    print(f"{'='*55}")

    async with httpx.AsyncClient() as client:
        token = await step_login(client)
        if not token:
            print(f"\n{RED}Cannot continue — login failed.{RESET}\n")
            sys.exit(1)

        school_id = await step_create_school(client, token)
        if not school_id:
            sys.exit(1)

        parent_id, parent_phone = await step_create_parent(client, token)
        if not parent_id:
            sys.exit(1)

        student_id, wallet_id = await step_create_student(
            client, token, school_id, parent_id
        )
        if not student_id:
            sys.exit(1)

        wallet_id, balance_before = await step_get_wallet(
            client, token, student_id
        )

        reference_id = await step_initiate_topup(client, token, wallet_id)
        await step_fire_webhook(client, reference_id)
        await step_confirm_balance(client, token, student_id, balance_before)
        await step_idempotency(client, reference_id)
        await step_yo_sandbox(client)

    total        = len(results)
    passed_count = sum(1 for r in results if r[0] == "PASS")
    failed_count = sum(1 for r in results if r[0] == "FAIL")

    print(f"\n{'='*55}")
    print(f"  RESULTS")
    print(f"{'='*55}")
    print(f"  Total:  {total}")
    print(f"  {GREEN}Passed: {passed_count}{RESET}")
    print(f"  {RED}Failed: {failed_count}{RESET}")

    if failed_count == 0:
        print(f"\n  {GREEN}{BOLD}ALL TESTS PASSED — Integration working!{RESET}\n")
    else:
        print(f"\n  {RED}Failed tests:{RESET}")
        for s, n in results:
            if s == "FAIL":
                print(f"    x  {n}")
    print()
    return failed_count == 0


if __name__ == "__main__":
    success = asyncio.run(main())
    sys.exit(0 if success else 1)