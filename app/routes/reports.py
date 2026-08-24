# ================================================
# app/routes/reports.py
# ------------------------------------------------
# Phase 4 — Multi-vendor reporting and settlement
#
# ENDPOINTS:
# GET  /reports/merchant/{id}/daily      → merchant daily report
# GET  /reports/merchant/{id}/dashboard  → merchant summary
# GET  /reports/school/{id}/settlement   → admin settlement report
# POST /reports/school/{id}/payout       → trigger manual payout
# POST /reports/settlements/auto         → automated daily payout
# ================================================

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session
from sqlalchemy import func, or_, and_
from datetime import datetime, date, timedelta
from typing import Optional

from app.database import get_db
from app.models import Transaction, Wallet, Student, Merchant, School, User, Payout
from app.momo import disburse_to_merchant, verify_transaction
from app.auth import get_current_admin, assert_school_access
from app.models import User

router = APIRouter()


# ================================================
# PAYOUT STATUS RESOLUTION
# ------------------------------------------------
# Yo's TransactionStatus has exactly four values (confirmed with Yo):
# SUCCEEDED, PENDING, FAILED, INDETERMINATE. All four are mapped
# explicitly. A binary "OK / else" was wrong in BOTH directions:
#
#   * Status=OK + TransactionStatus=PENDING was recorded "sent" — we told
#     the admin money had landed when it had not.
#   * any non-OK Status was recorded "failed" and retried — including
#     PENDING/INDETERMINATE responses and plain timeouts, which is how the
#     same merchant gets paid twice.
#
# "PENDING" and "INDETERMINATE" collapse to one stored status because the
# only decision hanging off them is identical: do not re-send, poll. One
# value means a later edit cannot handle one and forget the other. The
# distinction survives in the returned reason string.
# ================================================
_YO_TX_STATUS_MAP = {
    "SUCCEEDED":     "sent",
    "FAILED":        "failed",          # safe to retry — money did not move
    "PENDING":       "indeterminate",   # do not retry — poll
    "INDETERMINATE": "indeterminate",   # do not retry — poll
}


# ================================================
# YO NUMERIC StatusCode (§ status code table)
# ------------------------------------------------
# StatusCode REFINES TransactionStatus; it never replaces it. Yo's own
# guidance for several codes ("consider this SUCCEEDED", "consider this
# FAILED") is written for a caller that can afford to be wrong once. We
# cannot: "failed" is the only status in this module that unlocks another
# send, so any code we are not certain about resolves to "indeterminate"
# and gets polled instead.
#
# DELIBERATE DEVIATIONS FROM THE SPEC (both approved by the owner):
#
#   3  Yo says "consider FAILED". We map it INDETERMINATE. If Yo is right
#      we lose nothing — the poll confirms FAILED and the retry proceeds
#      one cycle later. If Yo is wrong we avoid paying a merchant twice.
#      Asymmetric cost, so we take the slow side.
#
#   4  INDETERMINATE, which Yo says to consider SUCCEEDED. We follow Yo
#      here and map it "sent", because "sent" does NOT unlock a re-send —
#      it is the safe direction. Same for 6 (succeeded, balance lagging).
#
# PAYOUT ONLY. These codes must not be extended into topup.py, which
# credits real wallet balances and may act only on an unambiguous
# SUCCEEDED.
# ================================================
_YO_STATUS_CODE_MAP = {
    "6":   ("sent",          "succeeded — Yo balance not yet updated"),
    "4":   ("sent",          "INDETERMINATE — Yo says consider SUCCEEDED"),
    "-30": ("failed",        "transaction not found — never reached Yo"),
    "-13": ("failed",        "insufficient funds on the Yo account"),
    "-21": ("failed",        "IP address not permitted"),
    "-8":  ("indeterminate", "likely duplicate — an earlier attempt exists"),
    "-6":  ("indeterminate", "duplicate transaction code"),
    "-22": ("indeterminate", "requires extra authorization — spec: DO NOT RE-SUBMIT"),
    "3":   ("indeterminate", "spec says FAILED; we poll instead (deliberate deviation)"),
    "5":   ("indeterminate", "resolves within ~1 hour — poll"),
    "9":   ("indeterminate", "resolves within ~1 hour — poll"),
}


# A "pending" row older than this had its process die mid-send. Its
# reference is already recorded, so the resolver can poll it.
_STALE_PENDING_MINUTES = 10

# How many times the UNATTENDED cron may send for one (merchant, date).
# Only ever reached via Yo-confirmed FAILED, so every attempt under the cap
# is a send Yo told us did not move money. Past the cap we stop anyway: a
# merchant whose sends keep failing has something wrong with their MoMo
# account that another automated retry will not fix. The manual admin
# trigger is deliberately NOT capped — a human looking at the row decides.
_MAX_AUTOMATED_ATTEMPTS = 3


def _status_code_of(result: dict) -> str:
    """The numeric StatusCode as a bare string, or "" if Yo did not send one."""
    raw = result.get("StatusCode")
    return str(raw).strip() if raw is not None else ""


def _refine_by_status_code(
    status: str,
    reason: str,
    result: dict,
) -> tuple:
    """
    Let the numeric StatusCode sharpen a status already derived from
    TransactionStatus.

    HARD RULE: refinement may never on its own produce "failed". Reaching
    "failed" is what permits another send, and a numeric code sitting
    alongside a TransactionStatus is commentary on that status, not a
    contradiction of it. Codes that mean "failed" are only honoured by
    _classify_payout_result() when there is NO TransactionStatus at all,
    i.e. a request-level rejection where nothing was ever attempted.
    """
    code = _status_code_of(result)
    if not code or code not in _YO_STATUS_CODE_MAP:
        return status, reason

    refined, detail = _YO_STATUS_CODE_MAP[code]

    if refined == status:
        return status, reason

    if refined == "failed":
        # The code alone must not unlock a re-send. Yo gave us BOTH a
        # TransactionStatus that was not FAILED and a failure code — that
        # disagreement is exactly what polling is for.
        return "indeterminate", f"{reason} (StatusCode {code}: {detail}) — not retried"

    return refined, f"{reason} (StatusCode {code}: {detail})"


def _payout_narrative(merchant_name: str, payout_date: date) -> str:
    """
    The Narrative that Yo dedupes on. Nothing volatile: no now(), no uuid,
    no counter. payout_date is the row's settlement date, fixed for the
    life of the row — NOT today's date.

        "School Wallet payout Kampala Tuck Shop 2026-08-13"

    NOTE: merchant.name is mutable. Renaming a merchant between an
    unresolved attempt and a retry changes this string, so Yo would no
    longer recognise the retry as a duplicate. The DB status check is the
    primary guard and is unaffected; this is the secondary net only.
    Snapshotting the name onto the Payout row would fix it properly, but
    that needs a live ALTER, so it is not assumed here.
    """
    return f"School Wallet payout {merchant_name} {payout_date.isoformat()}"


def _payout_reference(merchant_id: int, payout_date: date, attempt: int) -> str:
    """
    The ExternalReference Yo dedupes on, and the PrivateTransactionReference
    we poll with. Deterministic per (merchant, date, attempt):

        "SW-PAYOUT-12-20260813-1"

    Deterministic per ATTEMPT, not per (merchant, date) forever. Yo rejects
    a withdrawal whose (Account, Amount, Narrative, ExternalReference) all
    four match an earlier one, so a legitimate retry after a CONFIRMED
    FAILED needs a tuple Yo has not seen — bumping the attempt is what
    makes that retry acceptable, while an accidental re-send of the SAME
    attempt stays byte-identical and gets refused by Yo.
    """
    return f"SW-PAYOUT-{merchant_id}-{payout_date.strftime('%Y%m%d')}-{attempt}"


def _next_attempt(payout: Payout) -> int:
    """
    Attempt number for the next send, read back off the last reference so
    no new column is needed on the live payouts table.

    A legacy row whose yo_reference is a bare uuid4 (written before
    references became deterministic) restarts at 1. That uuid is still
    pollable, so such a row should be resolved before it is ever retried.
    """
    if not payout.yo_reference:
        return 1
    tail = payout.yo_reference.rsplit("-", 1)[-1]
    return int(tail) + 1 if tail.isdigit() else 1


def _classify_payout_result(result: dict) -> tuple:
    """
    Turn a disburse_to_merchant() response into (payout_status, reason).
    Only "failed" ever permits another send.
    """
    delivery = result.get("_Delivery")
    msg      = result.get("StatusMessage", "")

    # Never left our process → money definitively did not move.
    if delivery == "not_sent":
        return "failed", f"not sent: {msg}"

    # Sent, but no usable answer came back (timeout / reset / bad body).
    if delivery == "unknown":
        return "indeterminate", f"no response from Yo — fate unknown: {msg}"

    tx = (result.get("TransactionStatus") or "").strip().upper()
    if tx in _YO_TX_STATUS_MAP:
        return _refine_by_status_code(
            _YO_TX_STATUS_MAP[tx], f"Yo reported {tx}", result
        )

    # Yo answered, but not with one of its four documented statuses.
    if tx:
        return _refine_by_status_code(
            "indeterminate", f"unrecognised TransactionStatus {tx!r}", result
        )

    # No TransactionStatus at all — a request-level rejection. This is the
    # ONLY place a numeric code may produce "failed" on its own, because
    # here there is no attempted transaction for it to contradict: Yo threw
    # the request out before it became one.
    code = _status_code_of(result)
    if code in _YO_STATUS_CODE_MAP:
        mapped, detail = _YO_STATUS_CODE_MAP[code]
        return mapped, f"Yo StatusCode {code}: {detail}" + (f" — {msg}" if msg else "")

    # An unmapped code with no TransactionStatus: we do not know what Yo
    # did with the request, so fail closed rather than unlocking a re-send.
    if code:
        return "indeterminate", f"unrecognised StatusCode {code!r}: {msg or 'no detail'}"

    status_field = (result.get("Status") or "").strip().upper()

    if status_field == "OK":
        return "indeterminate", "Yo returned OK with no TransactionStatus"

    # No Status field AT ALL — whatever came back is not a Yo answer. An
    # HTML error page from a proxy or WAF parses as XML perfectly well and
    # flattens to its own tags (title, body, ...), carrying none of Yo's.
    # httpx does not raise for HTTP status and disburse_to_merchant() does
    # not check response.status_code, so such a body arrives here marked
    # _Delivery="responded". The request reached SOMETHING; the money's
    # fate is unknown, and it must not be recorded "failed" — the one
    # status that unlocks another send.
    #
    # SCOPE: this catches a body that PARSED but is not a Yo answer. A
    # body that fails to parse at all (empty, truncated, plain text) never
    # reaches here — parse_yo_response() flags it _ParseFailed and
    # disburse_to_merchant() maps that to _Delivery="unknown", which
    # returns at the top of this function. Both halves are needed: without
    # the momo.py marker an unparseable body arrives wearing a
    # manufactured {"Status": "ERROR"} and is indistinguishable from a
    # genuine Yo rejection. See tests/test_payout_resolution_gaps.py.
    if not status_field:
        seen = sorted(k for k in result if not k.startswith("_"))
        return "indeterminate", (
            "response was not a Yo answer (no Status field) — fate unknown; "
            f"parsed fields: {seen[:5]}"
        )

    return "failed", msg or "Payout failed"


async def _resolve_payout(db: Session, payout: Payout) -> dict:
    """
    Ask Yo what happened to an unresolved payout, and write the answer down.

    Withdrawals have NO callbacks — actransactioncheckstatus is the only
    resolution path, so this is active polling and nothing resolves itself.

    Returns {"outcome": "sent" | "failed" | "unresolved", "reason": ...}.
    A "failed" here is the ONLY thing that unlocks a re-send.
    """
    if not payout.yo_reference:
        return {
            "outcome": "unresolved",
            "reason": "no reference recorded — cannot poll; resolve by hand",
        }

    result = await verify_transaction(payout.yo_reference)

    # Could not reach Yo, or Yo could not answer: this resolves NOTHING and
    # must not be read as FAILED.
    if result.get("_Delivery") != "responded" or result.get("Status") != "OK":
        return {
            "outcome": "unresolved",
            "reason": (
                "status check did not resolve: "
                f"{result.get('StatusMessage', 'no detail')}"
            ),
        }

    tx = (result.get("TransactionStatus") or "").strip().upper()

    # The numeric StatusCode refines the status check exactly as it refines a
    # send (see _refine_by_status_code). It matters more here: code 4 or 6 on
    # a still-PENDING check is Yo telling us the money landed and its own
    # balance simply has not caught up, and a code that disagrees with a
    # FAILED must not be allowed to hand back a re-send licence.
    if tx == "SUCCEEDED":
        status, reason = "sent", "Yo confirmed SUCCEEDED"
    elif tx == "FAILED":
        status, reason = "failed", "Yo confirmed FAILED"
    elif tx:
        status, reason = "indeterminate", f"Yo still reports {tx}"
    else:
        status, reason = "indeterminate", "status check returned no TransactionStatus"

    status, reason = _refine_by_status_code(status, reason, result)

    if status == "sent":
        payout.status = "sent"
        payout.completed_at = datetime.utcnow()
        db.commit()
        return {"outcome": "sent", "reason": reason}

    if status == "failed":
        payout.status = "failed"
        payout.completed_at = datetime.utcnow()
        db.commit()
        return {"outcome": "failed", "reason": f"{reason} — retry permitted"}

    # Anything else: leave the row alone and poll again later.
    return {"outcome": "unresolved", "reason": reason}


# ================================================
# Idempotent per-merchant payout — the shared core behind both
# trigger_manual_payout() and automated_daily_payout(). See the Payout
# model's docstring in app/models.py for why the reservation row is
# inserted and committed BEFORE disburse_to_merchant() is ever called.
# ================================================
async def _process_merchant_payout(
    db: Session,
    merchant: Merchant,
    target_date: date,
    automated: bool = False,
) -> dict:
    """
    Pay out one merchant's completed sales for target_date, exactly
    once no matter how many times this is called for the same
    (merchant, target_date) — including genuinely concurrent calls.

    Returns a dict with an "outcome" key: "sent" | "skipped" | "failed"
    | "indeterminate" | "needs_human" | "no_sales" | "no_phone".

    Only "failed" is ever re-sent. "indeterminate" is resolved by polling,
    never by re-sending.

    automated=True marks the unattended cron path, which is capped at
    _MAX_AUTOMATED_ATTEMPTS sends per (merchant, target_date) and logs each
    re-send distinctly. The manual admin path is uncapped.
    """
    if not merchant.momo_phone:
        return {"merchant": merchant.name, "outcome": "no_phone", "reason": "No MoMo phone number set"}

    txns = (
        db.query(Transaction)
        .filter(
            Transaction.merchant_id == merchant.id,
            Transaction.type == "payment",
            Transaction.status == "completed",
        )
        .all()
    )
    day_txns = [t for t in txns if t.timestamp and t.timestamp.date() == target_date]
    merchant_total = sum(t.amount for t in day_txns)

    if merchant_total == 0:
        return {"merchant": merchant.name, "outcome": "no_sales", "reason": "No sales — nothing to pay out"}

    # Lock any existing row for this (merchant, date) FIRST — a
    # concurrent retry of a "failed" row needs this to serialize
    # correctly, the same reasoning as topup.py's check_topup_status().
    existing = (
        db.query(Payout)
        .filter(Payout.merchant_id == merchant.id, Payout.payout_date == target_date)
        .with_for_update()
        .first()
    )

    if existing and existing.status in ("pending", "sent"):
        return {
            "merchant": merchant.name, "outcome": "skipped",
            "reason": f"Already {existing.status} for {target_date}",
            "amount_ugx": existing.amount,
        }

    # An unresolved attempt exists — Yo may already have paid this merchant.
    # Poll before even considering another send. This is the hook-in point
    # for actransactioncheckstatus; withdrawals have no callbacks, so this
    # is the only thing that can move the row off "indeterminate".
    if existing and existing.status == "indeterminate":
        resolution = await _resolve_payout(db, existing)

        if resolution["outcome"] == "sent":
            return {
                "merchant": merchant.name, "outcome": "skipped",
                "reason": f"Already paid for {target_date} ({resolution['reason']})",
                "amount_ugx": existing.amount,
                "reference": existing.yo_reference,
            }

        if resolution["outcome"] == "unresolved":
            return {
                "merchant": merchant.name, "outcome": "indeterminate",
                "reason": resolution["reason"],
                "amount_ugx": existing.amount,
                "reference": existing.yo_reference,
            }

        # resolution["outcome"] == "failed" — Yo has now explicitly
        # CONFIRMED the money did not move. Fall through to a re-send with
        # a bumped attempt number. This is the ONLY route to a re-send.

    if existing is None:
        payout = Payout(
            merchant_id=merchant.id, payout_date=target_date,
            amount=merchant_total, status="pending",
        )
        db.add(payout)
        try:
            db.commit()
        except IntegrityError:
            # A concurrent request reserved this (merchant, date) first —
            # belt-and-suspenders behind the row lock above, same shape
            # as payments.py's nfc_payment() IntegrityError handling.
            db.rollback()
            return {
                "merchant": merchant.name, "outcome": "skipped",
                "reason": "A concurrent payout attempt claimed this merchant/date first",
            }
        db.refresh(payout)
    else:
        # existing.status == "failed" — either from the start, or just
        # confirmed FAILED by _resolve_payout() above. Retry, same row.
        payout = existing

    # Read the attempt number BEFORE yo_reference is overwritten below.
    attempt = _next_attempt(payout)

    # Cap the UNATTENDED path. Checked before the row is touched, so a
    # capped row keeps its "failed" status and stays visible — and stays
    # retryable by the manual admin trigger, which is what "needs a human"
    # means here.
    if automated and attempt > _MAX_AUTOMATED_ATTEMPTS:
        print(
            f"🛑 Payout attempt cap reached — NOT sending: {merchant.name} "
            f"for {target_date}, attempt {attempt} > "
            f"{_MAX_AUTOMATED_ATTEMPTS}; last ref {payout.yo_reference}. "
            f"Needs a human."
        )
        return {
            "merchant": merchant.name, "outcome": "needs_human",
            "reason": (
                f"automated attempt cap reached "
                f"({_MAX_AUTOMATED_ATTEMPTS} sends for {target_date}, all "
                f"confirmed FAILED by Yo) — not re-sent automatically"
            ),
            "amount_ugx": merchant_total,
            "reference": payout.yo_reference,
            "attempt": attempt,
        }

    ext_ref   = _payout_reference(merchant.id, target_date, attempt)
    narrative = _payout_narrative(merchant.name, target_date)

    # Record what we are ABOUT to send, before sending it. The reference is
    # deterministic now, so we can — and that is what makes a process death
    # mid-send recoverable: the row names the reference the resolver needs.
    payout.status = "pending"
    payout.amount = merchant_total
    payout.yo_reference = ext_ref
    payout.completed_at = None
    db.commit()

    if automated and attempt > 1:
        # Logged distinctly so a repeating pattern is visible in the cron
        # output rather than buried among first-time sends.
        print(
            f"🔁 AUTOMATED RE-SEND {attempt}/{_MAX_AUTOMATED_ATTEMPTS}: "
            f"{merchant.name} UGX {merchant_total:,} for {target_date} "
            f"(previous attempt confirmed FAILED by Yo) — ref {ext_ref}"
        )

    try:
        result = await disburse_to_merchant(
            phone=merchant.momo_phone, amount=merchant_total, merchant_name=merchant.name,
            external_reference=ext_ref, narrative=narrative,
        )
    except Exception as e:
        # momo.py catches its own network errors, so reaching here means
        # something unexpected — and we still do not know whether the
        # request went out. Unknown, not failed.
        payout.status = "indeterminate"
        payout.completed_at = None
        db.commit()
        return {
            "merchant": merchant.name, "outcome": "indeterminate",
            "reason": f"unexpected error, fate unknown: {e}",
            "amount_ugx": merchant_total, "reference": ext_ref,
        }

    new_status, reason = _classify_payout_result(result)

    payout.status = new_status
    payout.completed_at = datetime.utcnow() if new_status in ("sent", "failed") else None
    db.commit()

    if new_status == "sent":
        return {
            "merchant": merchant.name, "outcome": "sent",
            "amount_ugx": merchant_total, "reference": ext_ref,
        }

    if new_status == "failed":
        return {
            "merchant": merchant.name, "outcome": "failed",
            "reason": reason, "reference": ext_ref,
        }

    return {
        "merchant": merchant.name, "outcome": "indeterminate",
        "reason": reason, "amount_ugx": merchant_total, "reference": ext_ref,
    }


# ================================================
# ENDPOINT — resolve unresolved payouts (active polling)
# POST /reports/school/{school_id}/payouts/resolve
#
# Required, not optional. Withdrawals fire no callbacks, and the daily
# trigger only ever revisits TODAY's rows — so an indeterminate row from
# yesterday would never be looked at again by anything else.
# ================================================
@router.post("/school/{school_id}/payouts/resolve")
async def resolve_unresolved_payouts(
    school_id: int,
    db: Session = Depends(get_db),
    current_admin: User = Depends(get_current_admin),
):
    """
    Poll Yo for every payout of this school whose fate is unknown:
    status "indeterminate", plus "pending" rows old enough that their send
    cannot still be in flight.

    Read-only against Yo. Writes only status/completed_at, and only when Yo
    gives a definitive SUCCEEDED or FAILED.
    """
    school = db.query(School).filter(School.id == school_id).first()
    if not school:
        raise HTTPException(status_code=404, detail="School not found")

    assert_school_access(current_admin, school_id)

    stale_before = datetime.utcnow() - timedelta(minutes=_STALE_PENDING_MINUTES)

    rows = (
        db.query(Payout)
        .join(Merchant, Payout.merchant_id == Merchant.id)
        .filter(
            Merchant.school_id == school_id,
            or_(
                Payout.status == "indeterminate",
                and_(
                    Payout.status == "pending",
                    # A NULL created_at is MORE suspicious than an old one,
                    # not less — excluding it would make the row permanently
                    # invisible to the very endpoint built to catch stuck
                    # rows. created_at is a Python-side default, so a row
                    # inserted outside the ORM can legitimately have none.
                    or_(
                        Payout.created_at.is_(None),
                        Payout.created_at < stale_before,
                    ),
                ),
            ),
        )
        .all()
    )

    resolved_sent, resolved_failed, still_unresolved = [], [], []

    for payout in rows:
        outcome = await _resolve_payout(db, payout)
        entry = {
            "merchant_id": payout.merchant_id,
            "payout_date": str(payout.payout_date),
            "amount_ugx":  payout.amount,
            "reference":   payout.yo_reference,
            "reason":      outcome["reason"],
        }
        if outcome["outcome"] == "sent":
            resolved_sent.append(entry)
        elif outcome["outcome"] == "failed":
            resolved_failed.append(entry)
        else:
            still_unresolved.append(entry)

    return {
        "school":           school.name,
        "checked":          len(rows),
        "resolved_sent":    len(resolved_sent),
        "resolved_failed":  len(resolved_failed),
        "still_unresolved": len(still_unresolved),
        "details": {
            "sent":       resolved_sent,
            "failed":     resolved_failed,
            "unresolved": still_unresolved,
        },
        "note": (
            "'failed' rows are now retryable via the payout trigger. "
            "'unresolved' rows must NOT be re-sent — poll again later."
        ),
    }


# ================================================
# ENDPOINT 1 — Merchant daily sales report
# ================================================
# GET /reports/merchant/{merchant_id}/daily
#
# Shows a merchant exactly what they sold today
# or on any specific date.
# ================================================
@router.get("/merchant/{merchant_id}/daily")
def merchant_daily_report(
    merchant_id: int,
    report_date: Optional[str] = Query(
        default=None,
        description="Date in YYYY-MM-DD format. Defaults to today."
    ),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_admin)

):
    """
    Full daily sales report for a merchant.
    Shows every transaction, totals, and comparison to yesterday.
    """
    # ── Get merchant ────────────────────────────
    merchant = db.query(Merchant).filter(
        Merchant.id == merchant_id
    ).first()
    if not merchant:
        raise HTTPException(status_code=404, detail="Merchant not found")

    # ── Parse date ──────────────────────────────
    if report_date:
        try:
            target_date = datetime.strptime(report_date, "%Y-%m-%d").date()
        except ValueError:
            raise HTTPException(
                status_code=400,
                detail="Invalid date format. Use YYYY-MM-DD e.g. 2026-05-17"
            )
    else:
        target_date = date.today()

    yesterday = target_date - timedelta(days=1)

    # ── Get today's transactions ─────────────────
    today_txns = (
        db.query(Transaction)
        .filter(
            Transaction.merchant_id == merchant_id,
            Transaction.type == "payment",
            Transaction.status == "completed",
        )
        .all()
    )

    # Filter by date manually (SQLite compatible)
    today_txns = [
        t for t in today_txns
        if t.timestamp and t.timestamp.date() == target_date
    ]

    # ── Get yesterday's transactions ─────────────
    yesterday_txns = (
        db.query(Transaction)
        .filter(
            Transaction.merchant_id == merchant_id,
            Transaction.type == "payment",
            Transaction.status == "completed",
        )
        .all()
    )
    yesterday_txns = [
        t for t in yesterday_txns
        if t.timestamp and t.timestamp.date() == yesterday
    ]

    # ── Calculate totals ─────────────────────────
    today_total     = sum(t.amount for t in today_txns)
    yesterday_total = sum(t.amount for t in yesterday_txns)

    # ── Calculate change ─────────────────────────
    if yesterday_total > 0:
        change_pct = ((today_total - yesterday_total) / yesterday_total) * 100
        change_str = f"+{change_pct:.1f}%" if change_pct >= 0 else f"{change_pct:.1f}%"
    else:
        change_str = "N/A (no sales yesterday)"

    # ── Build transaction breakdown ──────────────
    breakdown = []
    for txn in sorted(today_txns, key=lambda x: x.timestamp, reverse=True):
        # Get student name
        wallet = db.query(Wallet).filter(Wallet.id == txn.wallet_id).first()
        student_name = "Unknown"
        if wallet:
            student = db.query(Student).filter(
                Student.id == wallet.student_id
            ).first()
            if student:
                student_name = student.name

        breakdown.append({
            "transaction_id": txn.id,
            "time":           txn.timestamp.strftime("%I:%M %p") if txn.timestamp else "N/A",
            "student":        student_name,
            "amount":         txn.amount,
            "description":    txn.description or "Payment",
        })

    # ── Busiest hour ─────────────────────────────
    if today_txns:
        hours = [t.timestamp.hour for t in today_txns if t.timestamp]
        if hours:
            busiest_hour = max(set(hours), key=hours.count)
            busiest_str  = f"{busiest_hour:02d}:00 - {busiest_hour:02d}:59"
        else:
            busiest_str = "N/A"
    else:
        busiest_str = "N/A"

    return {
        "merchant":          merchant.name,
        "merchant_id":       merchant_id,
        "school_id":         merchant.school_id,
        "report_date":       str(target_date),
        "summary": {
            "total_sales_ugx":        today_total,
            "number_of_transactions": len(today_txns),
            "average_transaction_ugx": round(today_total / len(today_txns)) if today_txns else 0,
            "busiest_hour":           busiest_str,
        },
        "comparison": {
            "today_ugx":     today_total,
            "yesterday_ugx": yesterday_total,
            "change":        change_str,
        },
        "transactions": breakdown,
        "payout_status": {
            "amount_to_receive": today_total,
            "payout_phone":      merchant.momo_phone,
            "note": "Payout sent daily at 6:00 PM automatically"
        }
    }


# ================================================
# ENDPOINT 2 — Merchant dashboard summary
# ================================================
# GET /reports/merchant/{merchant_id}/dashboard
#
# Overview of today, this week, and this month.
# ================================================
@router.get("/merchant/{merchant_id}/dashboard")
def merchant_dashboard(
    merchant_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_admin)

):
    """
    Full merchant dashboard summary.
    Shows today, this week, and this month at a glance.
    """
    merchant = db.query(Merchant).filter(
        Merchant.id == merchant_id
    ).first()
    if not merchant:
        raise HTTPException(status_code=404, detail="Merchant not found")

    today      = date.today()
    week_start = today - timedelta(days=today.weekday())
    month_start = today.replace(day=1)

    # Get all completed payments for this merchant
    all_txns = (
        db.query(Transaction)
        .filter(
            Transaction.merchant_id == merchant_id,
            Transaction.type == "payment",
            Transaction.status == "completed",
        )
        .all()
    )

    # Filter by period
    today_txns  = [t for t in all_txns if t.timestamp and t.timestamp.date() == today]
    week_txns   = [t for t in all_txns if t.timestamp and t.timestamp.date() >= week_start]
    month_txns  = [t for t in all_txns if t.timestamp and t.timestamp.date() >= month_start]

    # Totals
    today_total = sum(t.amount for t in today_txns)
    week_total  = sum(t.amount for t in week_txns)
    month_total = sum(t.amount for t in month_txns)

    # Daily breakdown for the week
    weekly_breakdown = []
    for i in range(7):
        day = week_start + timedelta(days=i)
        day_txns  = [t for t in all_txns if t.timestamp and t.timestamp.date() == day]
        day_total = sum(t.amount for t in day_txns)
        weekly_breakdown.append({
            "day":         day.strftime("%A %d %b"),
            "total_ugx":   day_total,
            "num_sales":   len(day_txns),
        })

    return {
        "merchant":    merchant.name,
        "merchant_id": merchant_id,
        "as_of":       str(today),
        "today": {
            "total_ugx":   today_total,
            "num_sales":   len(today_txns),
        },
        "this_week": {
            "total_ugx":   week_total,
            "num_sales":   len(week_txns),
            "daily_breakdown": weekly_breakdown,
        },
        "this_month": {
            "total_ugx":   month_total,
            "num_sales":   len(month_txns),
        },
        "payout_phone": merchant.momo_phone,
    }


# ================================================
# ENDPOINT 3 — School admin settlement report
# ================================================
# GET /reports/school/{school_id}/settlement
#
# Shows ALL vendors, their sales, and what to
# pay each one. The bursar's end-of-day view.
# ================================================
@router.get("/school/{school_id}/settlement")
def school_settlement_report(
    school_id: int,
    report_date: Optional[str] = Query(
        default=None,
        description="Date in YYYY-MM-DD format. Defaults to today."
    ),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_admin)

):
    """
    Full settlement report for school admin.
    Shows every vendor's sales and payout amounts.
    """
    # ── Get school ──────────────────────────────
    school = db.query(School).filter(School.id == school_id).first()
    if not school:
        raise HTTPException(status_code=404, detail="School not found")

    # ── Parse date ──────────────────────────────
    if report_date:
        try:
            target_date = datetime.strptime(report_date, "%Y-%m-%d").date()
        except ValueError:
            raise HTTPException(
                status_code=400,
                detail="Invalid date format. Use YYYY-MM-DD"
            )
    else:
        target_date = date.today()

    # ── Get all merchants for this school ────────
    merchants = db.query(Merchant).filter(
        Merchant.school_id == school_id,
        Merchant.is_active == True,
    ).all()

    if not merchants:
        return {
            "school":       school.name,
            "report_date":  str(target_date),
            "message":      "No active merchants found for this school"
        }

    # ── Build report for each merchant ──────────
    vendor_reports = []
    grand_total    = 0

    for merchant in merchants:
        # Get this merchant's transactions for the day
        txns = (
            db.query(Transaction)
            .filter(
                Transaction.merchant_id == merchant.id,
                Transaction.type == "payment",
                Transaction.status == "completed",
            )
            .all()
        )

        day_txns = [
            t for t in txns
            if t.timestamp and t.timestamp.date() == target_date
        ]

        merchant_total = sum(t.amount for t in day_txns)
        grand_total   += merchant_total

        # Full transaction breakdown
        transaction_details = []
        for t in sorted(day_txns, key=lambda x: x.timestamp, reverse=True):
            wallet = db.query(Wallet).filter(Wallet.id == t.wallet_id).first()
            student_name = "Unknown"
            if wallet:
                student = db.query(Student).filter(
                    Student.id == wallet.student_id
                ).first()
                if student:
                    student_name = student.name

            transaction_details.append({
                "time":        t.timestamp.strftime("%I:%M %p") if t.timestamp else "N/A",
                "student":     student_name,
                "amount_ugx":  t.amount,
                "description": t.description or "Payment",
            })

        vendor_reports.append({
            "merchant_id":        merchant.id,
            "merchant_name":      merchant.name,
            "payout_phone":       merchant.momo_phone,
            "total_sales_ugx":    merchant_total,
            "number_of_sales":    len(day_txns),
            "payout_status":      "pending",
            "transactions":       transaction_details,
        })

    return {
        "school":        school.name,
        "school_id":     school_id,
        "report_date":   str(target_date),
        "grand_total_ugx": grand_total,
        "number_of_vendors": len(merchants),
        "vendors":       vendor_reports,
        "generated_at":  datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC"),
    }


# ================================================
# ENDPOINT 4 — Manual payout trigger
# ================================================
# POST /reports/school/{school_id}/payout
#
# Admin manually triggers payout to all vendors.
# Uses DGateway disburse to send money to each
# merchant's MoMo number.
# ================================================
@router.post("/school/{school_id}/payout")
async def trigger_manual_payout(
    school_id: int,
    report_date: Optional[str] = Query(default=None),
    db: Session = Depends(get_db),
    current_admin: User = Depends(get_current_admin),
):
    """
    Trigger end-of-day payout to all merchants.
    Sends each vendor's daily sales to their MoMo number, exactly once
    per merchant per day — see _process_merchant_payout().
    Can also be triggered automatically at 6PM.
    """
    school = db.query(School).filter(School.id == school_id).first()
    if not school:
        raise HTTPException(status_code=404, detail="School not found")

    assert_school_access(current_admin, school_id)

    # Parse date
    if report_date:
        try:
            target_date = datetime.strptime(report_date, "%Y-%m-%d").date()
        except ValueError:
            raise HTTPException(status_code=400, detail="Invalid date format")
    else:
        target_date = date.today()

    merchants = db.query(Merchant).filter(
        Merchant.school_id == school_id,
        Merchant.is_active == True,
    ).all()

    payouts_sent       = []
    payouts_skipped    = []
    payouts_failed     = []
    payouts_unresolved = []

    for merchant in merchants:
        # automated=False: a human triggered this, so no attempt cap.
        outcome = await _process_merchant_payout(db, merchant, target_date)

        if outcome["outcome"] == "sent":
            payouts_sent.append({
                "merchant":   outcome["merchant"],
                "phone":      merchant.momo_phone,
                "amount_ugx": outcome["amount_ugx"],
                "status":     "sent",
                "reference":  outcome.get("reference", "N/A"),
            })
            print(f"✅ Payout sent: {merchant.name} UGX {outcome['amount_ugx']:,}")
        elif outcome["outcome"] == "skipped":
            payouts_skipped.append({
                "merchant": outcome["merchant"],
                "reason":   outcome["reason"],
            })
        elif outcome["outcome"] == "indeterminate":
            # Deliberately NOT in payouts_failed. An operator who reads
            # "failed" re-triggers — and on an unknown payout that is how
            # the merchant gets paid twice.
            payouts_unresolved.append({
                "merchant":   outcome["merchant"],
                "amount_ugx": outcome.get("amount_ugx", 0),
                "reference":  outcome.get("reference"),
                "reason":     outcome["reason"],
            })
            print(
                f"⚠️  Payout UNRESOLVED (do NOT re-trigger): {merchant.name} "
                f"— {outcome['reason']}"
            )
        else:
            payouts_failed.append({
                "merchant": outcome["merchant"],
                "reason":   outcome["reason"],
            })
            if outcome["outcome"] == "failed":
                print(f"❌ Payout failed: {merchant.name} — {outcome['reason']}")

    total_paid = sum(p["amount_ugx"] for p in payouts_sent)

    return {
        "school":        school.name,
        "payout_date":   str(target_date),
        # Counts payouts_sent only. Unresolved money is not claimed as
        # paid — and is not claimed as unpaid either.
        "total_paid_ugx": total_paid,
        "payouts_sent":  len(payouts_sent),
        "payouts_skipped": len(payouts_skipped),
        "payouts_failed": len(payouts_failed),
        "payouts_unresolved": len(payouts_unresolved),
        "details": {
            "sent":    payouts_sent,
            "skipped": payouts_skipped,
            "failed":  payouts_failed,
            "unresolved": payouts_unresolved,
        }
    }


# ================================================
# ENDPOINT 5 — Automated daily payout
# ================================================
# POST /reports/settlements/auto
#
# Called automatically at 6PM every day.
# Processes payouts for ALL schools at once.
# ================================================
@router.post("/settlements/auto")
async def automated_daily_payout(
    secret: str = Query(..., description="Secret key to prevent unauthorized calls"),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_admin)

):
    """
    Automated end-of-day payout for ALL schools.
    Called by a cron job at 6PM every day.
    Requires secret key for security.
    """
    import os
    expected_secret = os.getenv("SETTLEMENT_SECRET", "school_wallet_settle_2026")

    if secret != expected_secret:
        raise HTTPException(
            status_code=403,
            detail="Invalid secret key"
        )

    today = date.today()

    # Get all active schools
    schools = db.query(School).all()

    results = []

    for school in schools:
        merchants = db.query(Merchant).filter(
            Merchant.school_id == school.id,
            Merchant.is_active == True,
        ).all()

        school_total = 0
        school_payouts = []

        for merchant in merchants:
            # automated=True: unattended, so the attempt cap applies and
            # re-sends are logged distinctly.
            outcome = await _process_merchant_payout(
                db, merchant, today, automated=True
            )

            # Matches the original behavior: a merchant with no phone or
            # no sales isn't reported at all, just silently skipped.
            if outcome["outcome"] in ("no_phone", "no_sales"):
                continue

            if outcome["outcome"] == "sent":
                school_total += outcome["amount_ugx"]
                school_payouts.append({
                    "merchant":   outcome["merchant"],
                    "amount_ugx": outcome["amount_ugx"],
                    "status":     "sent",
                })
            else:
                school_payouts.append({
                    "merchant":   outcome["merchant"],
                    "amount_ugx": outcome.get("amount_ugx", 0),
                    # "skipped" | "failed" | "indeterminate" | "needs_human".
                    # Passed through verbatim — "indeterminate" and
                    # "needs_human" must reach whoever reads this, never be
                    # flattened into "failed".
                    "status":     outcome["outcome"],
                    "reason":     outcome.get("reason"),
                })

        results.append({
            "school":      school.name,
            "total_ugx":   school_total,
            "payouts":     school_payouts,
            # Surfaced at the top of each school's block so the cron output
            # does not have to be read line by line to spot them.
            "needs_human": [
                p["merchant"] for p in school_payouts
                if p["status"] == "needs_human"
            ],
            "unresolved": [
                p["merchant"] for p in school_payouts
                if p["status"] == "indeterminate"
            ],
        })

    grand_total = sum(r["total_ugx"] for r in results)

    print(f"\n🏦 Auto settlement complete: UGX {grand_total:,} across {len(schools)} schools")

    return {
        "date":        str(today),
        "schools":     len(schools),
        "grand_total_ugx": grand_total,
        "results":     results,
        "completed_at": datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC"),
    }