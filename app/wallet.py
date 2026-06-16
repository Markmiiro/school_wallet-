from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.database import get_db
from app.models import Wallet, Transaction, Student
from app.auth import get_current_user, get_current_admin
from app.models import User

router = APIRouter()


# ── GET /wallets/{student_id} ─────────────────────
@router.get("/{student_id}")
def get_wallet(
    student_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)  # ← add this
):
    """Get wallet for a student. Parent can only see their own child."""
    student = db.query(Student).filter(Student.id == student_id).first()
    if not student:
        raise HTTPException(status_code=404, detail="Student not found")

    # Parent can only see their own child's wallet
    if current_user.role == "parent" and student.parent_id != current_user.id:
        raise HTTPException(
            status_code=403,
            detail="Access denied — this is not your child"
        )

    wallet = db.query(Wallet).filter(
        Wallet.student_id == student_id
    ).first()
    if not wallet:
        raise HTTPException(status_code=404, detail="Wallet not found")

    return {
        "student":    student.name,
        "wallet_id":  wallet.id,
        "balance":    wallet.balance,
        "is_active":  wallet.is_active,
    }


# ── GET /wallets/{student_id}/balance ────────────
@router.get("/{student_id}/balance")
def get_balance(
    student_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """Quick balance check. Parent can only check their own child."""
    student = db.query(Student).filter(Student.id == student_id).first()
    if not student:
        raise HTTPException(status_code=404, detail="Student not found")

    if current_user.role == "parent" and student.parent_id != current_user.id:
        raise HTTPException(
            status_code=403,
            detail="Access denied — this is not your child"
        )

    wallet = db.query(Wallet).filter(
        Wallet.student_id == student_id
    ).first()
    if not wallet:
        raise HTTPException(status_code=404, detail="Wallet not found")

    return {
        "student_id": student_id,
        "balance":    wallet.balance,
        "currency":   "UGX",
        "message":    f"Available balance: UGX {wallet.balance:,}"
    }


# ── GET /wallets/{student_id}/history ────────────
@router.get("/{student_id}/history")
def get_transaction_history(
    student_id: int,
    limit: int = 20,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """Transaction history. Parent can only see their own child."""
    student = db.query(Student).filter(Student.id == student_id).first()
    if not student:
        raise HTTPException(status_code=404, detail="Student not found")

    if current_user.role == "parent" and student.parent_id != current_user.id:
        raise HTTPException(
            status_code=403,
            detail="Access denied — this is not your child"
        )

    wallet = db.query(Wallet).filter(
        Wallet.student_id == student_id
    ).first()
    if not wallet:
        raise HTTPException(status_code=404, detail="Wallet not found")

    transactions = (
        db.query(Transaction)
        .filter(Transaction.wallet_id == wallet.id)
        .order_by(Transaction.timestamp.desc())
        .limit(limit)
        .all()
    )

    total_in  = sum(t.amount for t in transactions if t.type == "topup" and t.status == "completed")
    total_out = sum(t.amount for t in transactions if t.type == "payment" and t.status == "completed")

    return {
        "student_id":      student_id,
        "wallet_id":       wallet.id,
        "current_balance": wallet.balance,
        "currency":        "UGX",
        "summary": {
            "total_topped_up": total_in,
            "total_spent":     total_out,
            "num_transactions": len(transactions),
        },
        "transactions": [
            {
                "id":          t.id,
                "type":        t.type,
                "direction":   "⬆️ IN" if t.type == "topup" else "⬇️ OUT",
                "amount":      t.amount,
                "status":      t.status,
                "description": t.description,
                "date":        t.timestamp,
            }
            for t in transactions
        ]
    }


# ── PUT /wallets/{student_id}/limit ──────────────
@router.put("/{student_id}/limit")
def set_daily_limit(
    student_id: int,
    daily_limit: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """Parent sets daily spending limit for their child."""
    student = db.query(Student).filter(Student.id == student_id).first()
    if not student:
        raise HTTPException(status_code=404, detail="Student not found")

    # Only parent of this student or admin can set limit
    if current_user.role == "parent" and student.parent_id != current_user.id:
        raise HTTPException(
            status_code=403,
            detail="Access denied — this is not your child"
        )

    if daily_limit <= 0:
        raise HTTPException(status_code=400, detail="Daily limit must be greater than 0")

    wallet = db.query(Wallet).filter(
        Wallet.student_id == student_id
    ).first()
    if not wallet:
        raise HTTPException(status_code=404, detail="Wallet not found")

    old_limit      = wallet.daily_limit
    wallet.daily_limit = daily_limit
    db.commit()

    return {
        "message":   "Daily limit updated ✅",
        "student":   student.name,
        "old_limit": old_limit,
        "new_limit": daily_limit,
        "currency":  "UGX",
    }


# ── PUT /wallets/{student_id}/deactivate ─────────
@router.put("/{student_id}/deactivate")
def deactivate_wallet(
    student_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_admin)  # admins only
):
    """Deactivate a student wallet. Admins only."""
    wallet = db.query(Wallet).filter(
        Wallet.student_id == student_id
    ).first()
    if not wallet:
        raise HTTPException(status_code=404, detail="Wallet not found")

    wallet.is_active = False
    db.commit()

    return {
        "message":          "Wallet deactivated ✅",
        "student_id":       student_id,
        "balance_preserved": wallet.balance,
    }


# ── PUT /wallets/{student_id}/reactivate ─────────
@router.put("/{student_id}/reactivate")
def reactivate_wallet(
    student_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_admin)  # admins only
):
    """Reactivate a deactivated wallet. Admins only."""
    wallet = db.query(Wallet).filter(
        Wallet.student_id == student_id
    ).first()
    if not wallet:
        raise HTTPException(status_code=404, detail="Wallet not found")

    wallet.is_active = True
    db.commit()

    return {
        "message":   "Wallet reactivated ✅",
        "student_id": student_id,
        "balance":   wallet.balance,
    }