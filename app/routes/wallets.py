from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from app.database import get_db
from app.models import Wallet, Transaction, Student, User
from app.auth import get_current_user, assert_school_access
from app.permissions import assert_wallet_access

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


# ================================================
# PUT /wallets/{student_id}/limit
# Set the daily spending limit for a child's wallet.
#
# Callable by the child's own parent, or by an admin scoped to the
# child's school (or a super admin). Merchants cannot change limits.
# The limit itself is enforced at charge time in app/routes/payments.py.
# ================================================
@router.put("/{student_id}/limit")
def set_daily_limit(
    student_id: int,
    daily_limit: int = Query(..., ge=MIN_DAILY_LIMIT, le=MAX_DAILY_LIMIT),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Set how much a child may spend per day, in UGX."""
    student = db.query(Student).filter(Student.id == student_id).first()
    if not student:
        raise HTTPException(status_code=404, detail="Student not found")

    if current_user.role == "admin":
        assert_school_access(current_user, student.school_id)
    elif current_user.role == "parent":
        if student.parent_id != current_user.id:
            raise HTTPException(status_code=403, detail="Not permitted")
    else:
        raise HTTPException(status_code=403, detail="Not permitted")

    wallet = db.query(Wallet).filter(
        Wallet.student_id == student_id
    ).with_for_update().first()
    if not wallet:
        raise HTTPException(status_code=404, detail="Wallet not found")

    old_limit = wallet.daily_limit
    wallet.daily_limit = daily_limit
    db.commit()

    return {
        "message": "Daily limit updated",
        "student_id": student_id,
        "student": student.name,
        "old_limit": old_limit,
        "daily_limit": daily_limit,
        "currency": "UGX",
    }
