from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.database import get_db
from app.models import Wallet, Transaction, Student, User, ControlChange
from app.auth import get_current_user, assert_school_access
from app.permissions import assert_wallet_access
from app.controls import card_state, record_change
from app.routes.auth import confirm_pin
from app.routes.payments import _spent_today

router = APIRouter()

# Bounds for a parent- or admin-set daily spending limit, in UGX.
MIN_DAILY_LIMIT = 500
MAX_DAILY_LIMIT = 5_000_000


# ==========================================
# GET STUDENT WALLET
# ==========================================
# This endpoint returns a student's wallet
#
# Example:
# GET /wallets/1
#
# Meaning:
# "Show me the wallet for student ID 1"
#
# NOTE: this route is written as "/wallets/{student_id}" AND the router
# is mounted with prefix="/wallets" in main.py, so the live path is
# actually /wallets/wallets/{student_id}. This is a known quirk. The
# Flutter app depends on it — do NOT "fix" the path here without
# updating the app's ApiConstants at the same time, or the app breaks.
#
# Requires a token. A parent sees only their own child; staff see only
# students of their own school (see assert_wallet_access).
# ==========================================

@router.get("/wallets/{student_id}")
def get_wallet(
    student_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):

    # STEP 1 → confirm student exists, and that the caller may see them
    student = db.query(Student).filter(Student.id == student_id).first()

    if not student:
        raise HTTPException(
            status_code=404,
            detail="Student not found"
        )

    assert_wallet_access(current_user, student)

    # STEP 2 → find wallet
    wallet = db.query(Wallet).filter(
        Wallet.student_id == student_id
    ).first()

    # STEP 3 → confirm wallet exists
    if not wallet:
        raise HTTPException(
            status_code=404,
            detail="Wallet not found"
        )

    # STEP 4 → return wallet info
    return {
        "student": student.name,
        "wallet_id": wallet.id,
        "balance": wallet.balance,
        "is_active": wallet.is_active,
        "daily_limit": wallet.daily_limit,
    }

# ================================================
# GET /wallets/{student_id}/history
# Full transaction history for a wallet
# ================================================
@router.get("/{student_id}/history")
def get_transaction_history(
    student_id: int,
    limit: int = Query(default=20, ge=1, le=100),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """
    Get all transactions for a student's wallet.
    Shows both top-ups (money IN) and payments (money OUT).
    Ordered newest first.
    """
    student = db.query(Student).filter(Student.id == student_id).first()
    if not student:
        raise HTTPException(
            status_code=404,
            detail=f"No wallet found for student {student_id}"
        )

    assert_wallet_access(current_user, student)

    # Find wallet
    wallet = db.query(Wallet).filter(
        Wallet.student_id == student_id
    ).first()
    if not wallet:
        raise HTTPException(
            status_code=404,
            detail=f"No wallet found for student {student_id}"
        )

    # Get all transactions newest first
    transactions = (
        db.query(Transaction)
        .filter(Transaction.wallet_id == wallet.id)
        .order_by(Transaction.timestamp.desc())
        .limit(limit)
        .all()
    )

    # Calculate totals
    total_in = sum(
        t.amount for t in transactions
        if t.type == "topup" and t.status == "completed"
    )
    total_out = sum(
        t.amount for t in transactions
        if t.type == "payment" and t.status == "completed"
    )

    return {
        "student_id": student_id,
        "wallet_id": wallet.id,
        "current_balance": wallet.balance,
        "is_active": wallet.is_active,
        "daily_limit": wallet.daily_limit,
        "currency": "UGX",
        "summary": {
            "total_topped_up": total_in,
            "total_spent": total_out,
            "number_of_transactions": len(transactions),
        },
        "transactions": [
            {
                "id": t.id,
                "type": t.type,
                # Plain "IN"/"OUT" (no emoji arrows) — cleaner for the
                # app to parse. The app already handles both forms.
                "direction": "IN" if t.type == "topup" else "OUT",
                "amount": t.amount,
                "status": t.status,
                "reference": t.reference,
                "description": t.description,
                "date": t.timestamp,
            }
            for t in transactions
        ]
    }


def _assert_controls_access(user: User, student: Student) -> None:
    """The child's own parent, or an admin of the child's school. Not till staff."""
    if user.role == "admin":
        assert_school_access(user, student.school_id)
    elif user.role == "parent":
        if student.parent_id != user.id:
            raise HTTPException(status_code=403, detail="Not permitted")
    else:
        raise HTTPException(status_code=403, detail="Not permitted")


def _who(change: ControlChange, viewer: User) -> str:
    if change.actor_user_id == viewer.id:
        return "you"
    return {"admin": "school", "parent": "parent"}.get(change.actor_role, change.actor_role)


# ================================================
# GET /wallets/{student_id}/controls
# Everything the Controls view shows: the daily limit, today's spend
# against it (Kampala calendar day, the same sum the payment path uses),
# the card's state, and the last 20 changes to any of it.
# ================================================
@router.get("/{student_id}/controls")
def get_controls(
    student_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    student = db.query(Student).filter(Student.id == student_id).first()
    if not student:
        raise HTTPException(status_code=404, detail="Student not found")
    _assert_controls_access(current_user, student)
    wallet = db.query(Wallet).filter(Wallet.student_id == student_id).first()
    if not wallet:
        raise HTTPException(status_code=404, detail="Wallet not found")

    spent = _spent_today(db, wallet.id)
    state, card = card_state(student)
    history = (
        db.query(ControlChange)
        .filter(ControlChange.student_id == student_id)
        .order_by(ControlChange.created_at.desc(), ControlChange.id.desc())
        .limit(20).all()
    )
    return {
        "student_id": student.id,
        "daily_limit": wallet.daily_limit,
        "spent_today": spent,
        "remaining_today": max(0, wallet.daily_limit - spent),
        "limit_min": MIN_DAILY_LIMIT,
        "limit_max": MAX_DAILY_LIMIT,
        "card": {
            "state": state,
            "last_digits": card.tag_uid[-4:] if card is not None and card.tag_uid else None,
            "can_block": state == "active",
            "can_unblock": state == "blocked",
        },
        "history": [{
            "by": _who(h, current_user),
            "control": h.control,
            "from": h.old_value,
            "to": h.new_value,
            "at": h.created_at.isoformat() + "Z",
        } for h in history],
    }


class LimitChange(BaseModel):
    daily_limit: int = Field(..., ge=MIN_DAILY_LIMIT, le=MAX_DAILY_LIMIT)
    pin: str


# ================================================
# PUT /wallets/{student_id}/limit
# Set the daily spending limit for a child's wallet.
#
# Callable by the child's own parent, or by an admin scoped to the
# child's school (or a super admin). Merchants cannot change limits.
# The limit itself is enforced at charge time in app/routes/payments.py.
#
# A money control: the body carries the caller's PIN, checked before
# anything changes (wrong PIN 400, counted toward the login lockout),
# and the change is audited in the same transaction.
# ================================================
@router.put("/{student_id}/limit")
def set_daily_limit(
    student_id: int,
    data: LimitChange,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Set how much a child may spend per day, in UGX."""
    student = db.query(Student).filter(Student.id == student_id).first()
    if not student:
        raise HTTPException(status_code=404, detail="Student not found")

    _assert_controls_access(current_user, student)
    confirm_pin(db, current_user, data.pin)
    daily_limit = data.daily_limit

    wallet = db.query(Wallet).filter(
        Wallet.student_id == student_id
    ).with_for_update().first()
    if not wallet:
        raise HTTPException(status_code=404, detail="Wallet not found")

    old_limit = wallet.daily_limit
    wallet.daily_limit = daily_limit
    current_user.failed_login_attempts = 0
    record_change(db, student, current_user, "daily_limit", old_limit, daily_limit)
    db.commit()

    return {
        "message": "Daily limit updated",
        "student_id": student_id,
        "student": student.name,
        "old_limit": old_limit,
        "daily_limit": daily_limit,
        "currency": "UGX",
    }
