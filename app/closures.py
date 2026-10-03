# ================================================
# app/closures.py
# ------------------------------------------------
# A parent closing (deleting) their account.
#
#   request ──► held (72 hours) ──► refund sent ──► completed
#
# At the request: every child's wallet is frozen and card closed, and the
# parent's phone on `users` is replaced with a marker. Tokens name the user
# by phone (app/auth.py), so that signs out every device at once without
# touching auth.py. The real number moves to the closure row and is used
# for one thing only: the refund.
#
# During the hold only an operator can cancel. The hold also lets a
# top-up already in flight settle before the balance is read.
#
# After the hold: the balance of every wallet, plus any card fee paid for
# a card never issued, is debited FIRST (recorded as "refund"
# transactions), then sent to the registered number with the payout rules
# from app/routes/reports.py: record the reference before sending; only a
# FAILED confirmed by Yo unlocks another send; anything unknown is polled.
# Only once Yo confirms the money moved is the person removed.
#
# What "removed" means — financial rows are never deleted:
#   users            name → "Deleted user", PIN removed; row kept (card
#                    orders point at it); terms acceptance kept as proof
#   students         name → "Closed account · {account number}", date of
#                    birth and class emptied; school and account number
#                    kept so the school's reports still add up
#   transactions,
#   card_orders      payer phone emptied (Yo can trace by reference)
#   pending_ussd_registrations for that phone: deleted
#   account_closures kept; refund_phone dropped after RETENTION_YEARS
# ================================================

import json
from datetime import datetime, timedelta

from sqlalchemy.orm import Session

from app.models import (
    AccountClosure, CardOrder, NFCTag, Student, Transaction, User, Wallet,
)
from app.momo import disburse_to_merchant, verify_transaction
from app.routes.reports import (
    _MAX_AUTOMATED_ATTEMPTS, _STALE_PENDING_MINUTES,
    _classify_payout_result, _refine_by_status_code,
)
from app.routes.ussd import PendingUssdRegistration
from app.sms import send_sms_sync

HOLD_HOURS = 72
REFUND_DAYS = 14
# How long the refund record keeps the phone it was paid to. Five years
# matches the published terms; the lawyer may say ten (see terms.py).
RETENTION_YEARS = 5
# A top-up started this recently may still be credited by the webhook, so
# the refund waits for it. Older pending ones were never approved.
PENDING_TOPUP_GRACE = timedelta(hours=24)

SUPPORT_PHONE = "0760 945 424"
SUPPORT_EMAIL = "markmiiro77@gmail.com"

OPEN_STATUSES = ("held", "ready", "pending", "failed", "indeterminate", "needs_human")


def _tombstone(user: User) -> str:
    return f"deleted-{user.id}"


def mask_phone(phone: str) -> str:
    return f"•••• {phone[-3:]}" if phone else ""


# ── What will happen ─────────────────────────────────
def _children(db: Session, user: User) -> list:
    return (
        db.query(Student).filter(Student.parent_id == user.id)
        .order_by(Student.id).all()
    )


def _unissued_orders(db: Session, user: User, student_ids: list) -> list:
    if not student_ids:
        return []
    return (
        db.query(CardOrder)
        .filter(CardOrder.student_id.in_(student_ids), CardOrder.status == "paid")
        .all()
    )


def preview(db: Session, user: User) -> dict:
    children = []
    for s in _children(db, user):
        wallet = s.wallet
        card = s.active_nfc_tag
        children.append({
            "name": s.name,
            "account_number": s.account_number,
            "balance": wallet.balance if wallet else 0,
            "has_working_card": bool(card and card.tag_uid),
        })
    orders = _unissued_orders(db, user, [s.id for s in _children(db, user)])
    card_fees = sum(o.amount for o in orders)
    total = sum(c["balance"] for c in children) + card_fees
    phone = mask_phone(user.phone)
    return {
        "children": children,
        "unissued_card_fees": card_fees,
        "refund_total": total,
        "refund_phone": phone,
        "hold_hours": HOLD_HOURS,
        "refund_days": REFUND_DAYS,
        "consequences": [
            "Your children's cards stop working at once.",
            "You are signed out on every device.",
            (f"UGX {total:,} will be sent to your registered number {phone} "
             f"within {REFUND_DAYS} days." if total else
             "There is no money to refund."),
            (f"For {HOLD_HOURS} hours you can still stop this by calling "
             f"{SUPPORT_PHONE}. After that it cannot be undone."),
            (f"Your name, your phone number and your children's details are "
             f"then removed. Payment records are kept for {RETENTION_YEARS} "
             f"years without your name, as the law requires."),
        ],
    }


def open_closure_for(db: Session, user: User):
    return (
        db.query(AccountClosure)
        .filter(AccountClosure.user_id == user.id,
                AccountClosure.status.in_(OPEN_STATUSES))
        .first()
    )


# ── The request ──────────────────────────────────────
def request_closure(db: Session, user: User, via: str) -> AccountClosure:
    """Freeze, sign out, and start the hold. The caller has checked the PIN."""
    now = datetime.utcnow()
    students = _children(db, user)
    student_ids = [s.id for s in students]

    frozen = []
    for wallet in db.query(Wallet).filter(Wallet.student_id.in_(student_ids)).all() \
            if student_ids else []:
        if wallet.is_active:
            wallet.is_active = False
            frozen.append(wallet.id)

    closed = {}
    for card in db.query(NFCTag).filter(NFCTag.student_id.in_(student_ids),
                                        NFCTag.is_active == True).all() \
            if student_ids else []:
        closed[str(card.id)] = card.status or "active"
        card.is_active = False
        card.status = "closed"
        card.deactivated_at = now

    phone = user.phone
    closure = AccountClosure(
        user_id=user.id, status="held", requested_via=via,
        refund_phone=phone,
        frozen_wallet_ids=json.dumps(frozen), closed_cards=json.dumps(closed),
        requested_at=now, process_after=now + timedelta(hours=HOLD_HOURS),
    )
    db.add(closure)
    user.phone = _tombstone(user)
    db.commit()
    db.refresh(closure)

    try:
        send_sms_sync(phone, (
            f"Your Nuvora account is closing. Any balance will be sent to this "
            f"number within {REFUND_DAYS} days. Didn't ask for this? Call "
            f"{SUPPORT_PHONE} within {HOLD_HOURS} hours."
        ))
    except Exception as e:   # the closure stands whether or not the SMS went
        print(f"⚠️ Closure {closure.id}: confirmation SMS failed: {e}")
    return closure


class CancelRefused(Exception):
    pass


def cancel(db: Session, closure: AccountClosure) -> None:
    """Undo a closure still in its hold. Restores exactly what it froze."""
    if closure.status != "held":
        raise CancelRefused(f"Closure is {closure.status}; only a held one can be cancelled.")
    user = db.get(User, closure.user_id)
    taken = db.query(User).filter(User.phone == closure.refund_phone,
                                  User.id != user.id).first()
    if taken:
        raise CancelRefused("That phone number now belongs to another account.")

    for wallet_id in json.loads(closure.frozen_wallet_ids or "[]"):
        wallet = db.get(Wallet, wallet_id)
        if wallet:
            wallet.is_active = True
    for card_id, previous in json.loads(closure.closed_cards or "{}").items():
        card = db.get(NFCTag, int(card_id))
        if card and card.status == "closed":
            card.is_active = True
            card.status = previous
            card.deactivated_at = None

    user.phone = closure.refund_phone
    closure.status = "cancelled"
    closure.cancelled_at = datetime.utcnow()
    db.commit()


# ── Processing (cron and operator) ───────────────────
def _student_ids(db: Session, closure: AccountClosure) -> list:
    return [s.id for s in db.query(Student)
            .filter(Student.parent_id == closure.user_id).all()]


def _topup_in_flight(db: Session, student_ids: list, now: datetime) -> bool:
    if not student_ids:
        return False
    wallet_ids = [w.id for w in db.query(Wallet)
                  .filter(Wallet.student_id.in_(student_ids)).all()]
    if wallet_ids and db.query(Transaction).filter(
        Transaction.wallet_id.in_(wallet_ids),
        Transaction.status == "pending",
        Transaction.timestamp > now - PENDING_TOPUP_GRACE,
    ).first():
        return True
    return db.query(CardOrder).filter(
        CardOrder.student_id.in_(student_ids),
        CardOrder.status == "pending",
        CardOrder.created_at > now - PENDING_TOPUP_GRACE,
    ).first() is not None


def _debit_everything(db: Session, closure: AccountClosure, student_ids: list) -> int:
    """
    Move every shilling owed into the closure, recorded, before any send
    (momo.py §4.1: debit first). Wallet rows are locked so a concurrent
    top-up credit cannot slip between the read and the zeroing.
    """
    total = 0
    wallets = (
        db.query(Wallet).filter(Wallet.student_id.in_(student_ids))
        .with_for_update().all()
    ) if student_ids else []
    for wallet in wallets:
        if wallet.balance > 0:
            db.add(Transaction(
                wallet_id=wallet.id, amount=wallet.balance, type="refund",
                status="completed",
                description="Balance refunded on account closure",
            ))
            total += wallet.balance
            wallet.balance = 0
        wallet.is_active = False
    for order in (db.query(CardOrder)
                  .filter(CardOrder.student_id.in_(student_ids),
                          CardOrder.status == "paid").all()
                  if student_ids else []):
        order.status = "refunded"
        total += order.amount
    closure.refund_amount = total
    return total


def _anonymise(db: Session, closure: AccountClosure) -> None:
    user = db.get(User, closure.user_id)
    user.name = "Deleted user"
    user.pin_hash = None
    user.failed_login_attempts = 0
    user.locked_until = None
    user.phone = _tombstone(user)

    students = db.query(Student).filter(Student.parent_id == user.id).all()
    student_ids = [s.id for s in students]
    for s in students:
        s.name = "Closed account" + (f" · {s.account_number}" if s.account_number else "")
        s.dob = None
        s.class_name = None

    if student_ids:
        wallet_ids = [w.id for w in db.query(Wallet)
                      .filter(Wallet.student_id.in_(student_ids)).all()]
        if wallet_ids:
            db.query(Transaction).filter(Transaction.wallet_id.in_(wallet_ids)) \
                .update({Transaction.momo_phone: None}, synchronize_session=False)
        db.query(CardOrder).filter(CardOrder.student_id.in_(student_ids)) \
            .update({CardOrder.momo_phone: ""}, synchronize_session=False)
    db.query(CardOrder).filter(CardOrder.ordered_by == user.id) \
        .update({CardOrder.momo_phone: ""}, synchronize_session=False)
    if closure.refund_phone:
        db.query(PendingUssdRegistration) \
            .filter(PendingUssdRegistration.phone == closure.refund_phone) \
            .delete(synchronize_session=False)

    closure.status = "completed"
    closure.completed_at = datetime.utcnow()
    db.commit()


async def _poll(db: Session, closure: AccountClosure) -> str:
    """Ask Yo what happened to the last send. Returns the new status."""
    if not closure.yo_reference:
        return closure.status
    result = await verify_transaction(closure.yo_reference)
    if result.get("_Delivery") != "responded" or result.get("Status") != "OK":
        return "indeterminate"
    tx = (result.get("TransactionStatus") or "").strip().upper()
    if tx == "SUCCEEDED":
        status, reason = "sent", "Yo confirmed SUCCEEDED"
    elif tx == "FAILED":
        status, reason = "failed", "Yo confirmed FAILED"
    else:
        status, reason = "indeterminate", f"Yo still reports {tx or 'nothing'}"
    status, _ = _refine_by_status_code(status, reason, result)
    return status


async def _send(db: Session, closure: AccountClosure) -> str:
    closure.attempts = (closure.attempts or 0) + 1
    closure.yo_reference = f"NUV-CLOSE-{closure.id}-{closure.attempts}"
    closure.status = "pending"
    closure.last_sent_at = datetime.utcnow()
    db.commit()   # the reference is on record before the money moves
    try:
        result = await disburse_to_merchant(
            phone=closure.refund_phone, amount=closure.refund_amount,
            merchant_name="Nuvora refund",
            external_reference=closure.yo_reference,
            narrative=f"Nuvora account closure refund {closure.id}",
        )
    except Exception as e:
        print(f"⚠️ Closure {closure.id}: refund send raised, fate unknown: {e}")
        return "indeterminate"
    status, _ = _classify_payout_result(result)
    return status


async def process(db: Session, closure: AccountClosure, *, automated: bool,
                  now: datetime) -> dict:
    """Move one closure as far as it can safely go right now."""
    out = {"closure_id": closure.id}

    if closure.status == "held":
        if closure.process_after > now:
            return {**out, "outcome": "held"}
        student_ids = _student_ids(db, closure)
        if _topup_in_flight(db, student_ids, now):
            return {**out, "outcome": "held", "reason": "a top-up is still in flight"}
        if _debit_everything(db, closure, student_ids) == 0:
            _anonymise(db, closure)
            return {**out, "outcome": "completed", "refund_ugx": 0}
        closure.status = "ready"
        db.commit()

    stale = now - timedelta(minutes=_STALE_PENDING_MINUTES)
    if closure.status == "pending" and closure.last_sent_at and closure.last_sent_at > stale:
        # Another run is sending right now; polling this early could read
        # "not found" as failed and pay twice.
        return {**out, "outcome": "pending", "reference": closure.yo_reference}
    if closure.status in ("indeterminate", "pending"):
        status = await _poll(db, closure)
        if status == "sent":
            closure.refunded_at = now
            _anonymise(db, closure)
            return {**out, "outcome": "completed", "refund_ugx": closure.refund_amount}
        if status != "failed":
            closure.status = "indeterminate"
            db.commit()
            return {**out, "outcome": "indeterminate", "reference": closure.yo_reference}
        closure.status = "failed"
        db.commit()

    if closure.status in ("ready", "failed", "needs_human"):
        if closure.status == "needs_human" and automated:
            return {**out, "outcome": "needs_human"}
        if automated and (closure.attempts or 0) >= _MAX_AUTOMATED_ATTEMPTS:
            closure.status = "needs_human"
            db.commit()
            return {**out, "outcome": "needs_human"}
        status = await _send(db, closure)
        if status == "sent":
            closure.refunded_at = now
            _anonymise(db, closure)
            return {**out, "outcome": "completed", "refund_ugx": closure.refund_amount}
        closure.status = status if status in ("failed", "indeterminate") else "indeterminate"
        db.commit()
        return {**out, "outcome": closure.status, "reference": closure.yo_reference}

    return {**out, "outcome": closure.status}


def _purge_expired(db: Session, now: datetime) -> None:
    cutoff = now - timedelta(days=365 * RETENTION_YEARS + RETENTION_YEARS // 4 + 1)
    for c in db.query(AccountClosure).filter(
        AccountClosure.status == "completed",
        AccountClosure.completed_at < cutoff,
        AccountClosure.refund_phone.isnot(None),
    ).all():
        c.refund_phone = None
    db.commit()


def _money_after_closure(db: Session) -> list:
    """
    Money that reached a wallet after its closure finished. USSD can still
    top up by account number: ussd.py and webhook.py (both frozen) do not
    check that a wallet is active. Surfaced every run until an operator
    refunds it by hand.
    """
    found = []
    for c in db.query(AccountClosure).filter(AccountClosure.status == "completed").all():
        student_ids = _student_ids(db, c)
        if not student_ids:
            continue
        for w in db.query(Wallet).filter(Wallet.student_id.in_(student_ids),
                                         Wallet.balance > 0).all():
            found.append({"closure_id": c.id, "outcome": "money_after_closure",
                          "wallet_id": w.id, "balance_ugx": w.balance})
    return found


async def process_due(db: Session, *, automated: bool) -> list:
    now = datetime.utcnow()
    results = []
    ids = [c.id for c in db.query(AccountClosure.id)
           .filter(AccountClosure.status.in_(OPEN_STATUSES))
           .order_by(AccountClosure.id).all()]
    for closure_id in ids:
        # Locked and re-read, so a cron run and an operator click cannot
        # both act on the same closure: the second sees what the first did.
        closure = (db.query(AccountClosure)
                   .filter(AccountClosure.id == closure_id)
                   .with_for_update().first())
        if closure.status not in OPEN_STATUSES:
            db.commit()
            continue
        results.append(await process(db, closure, automated=automated, now=now))
    _purge_expired(db, now)
    return results + _money_after_closure(db)
