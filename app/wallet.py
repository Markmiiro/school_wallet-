from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.database import get_db
from app.models import Wallet, Transaction, Student

router = APIRouter()


# ================================================
# GET /wallets/{student_id}
# Get wallet for a specific student
# ================================================
@router.get("/{student_id}")
def get_wallet(student_id: int, db: Session = Depends(get_db)):
    """
    Get the wallet belonging to a student.
    Use the student ID — not the wallet ID.
    Every student has exactly one wallet.
    """
    # Find the student first
    student = db.query(Student).filter(Student.id == student_id).first()
    if not student:
        raise HTTPException(
            status_code=404,
            detail=f"Student with ID {student_id} not found"
        )

    # Find their wallet
    wallet = db.query(Wallet).filter(
        Wallet.student_id == student_id
    ).first()
    if not wallet:
        raise HTTPException(
            status_code=404,
            detail=f"No wallet found for student {student_id}"
        )

    return {
        "wallet_id": wallet.id,
        "student_id": student_id,
        "student_name": student.name,
        "balance": wallet.balance,
        "currency": "UGX",
        "is_active": wallet.is_active,
    }


# ================================================
# GET /wallets/{student_id}/balance
# Quick balance check
# ================================================
@router.get("/{student_id}/balance")
def get_balance(student_id: int, db: Session = Depends(get_db)):
    """
    Quick balance check for a student's wallet.
    Returns just the balance — nothing else.
    Useful for the tuck shop screen showing
    how much a student has before they buy.
    """
    wallet = db.query(Wallet).filter(
        Wallet.student_id == student_id
    ).first()
    if not wallet:
        raise HTTPException(
            status_code=404,
            detail=f"No wallet found for student {student_id}"
        )

    # Block payment if wallet is deactivated
    if not wallet.is_active:
        raise HTTPException(
            status_code=403,
            detail="This wallet is deactivated. Contact school admin."
        )

    return {
        "student_id": student_id,
        "balance": wallet.balance,
        "currency": "UGX",
        "message": f"Available balance: UGX {wallet.balance:,}"
    }


# ================================================
# GET /wallets/{student_id}/history
# Full transaction history for a wallet
# ================================================
@router.get("/{student_id}/history")
def get_transaction_history(
    student_id: int,
    limit: int = 20,
    db: Session = Depends(get_db)
):
    """
    Get all transactions for a student's wallet.
    Ordered newest first.
    limit → how many to return (default 20)

    Shows both:
    - Top-ups (money coming IN)
    - Payments (money going OUT)
    """
    # Find wallet
    wallet = db.query(Wallet).filter(
        Wallet.student_id == student_id
    ).first()
    if not wallet:
        raise HTTPException(
            status_code=404,
            detail=f"No wallet found for student {student_id}"
        )

    # Get transactions newest first
    transactions = (
        db.query(Transaction)
        .filter(Transaction.wallet_id == wallet.id)
        .order_by(Transaction.timestamp.desc())
        .limit(limit)
        .all()
    )

    # Calculate totals
    total_topped_up = sum(
        t.amount for t in transactions
        if t.type == "topup" and t.status == "completed"
    )
    total_spent = sum(
        t.amount for t in transactions
        if t.type == "payment" and t.status == "completed"
    )

    return {
        "student_id": student_id,
        "wallet_id": wallet.id,
        "current_balance": wallet.balance,
        "currency": "UGX",
        "summary": {
            "total_topped_up": total_topped_up,
            "total_spent": total_spent,
            "number_of_transactions": len(transactions),
        },
        "transactions": [
            {
                "id": t.id,
                "type": t.type,            # "topup" or "payment"
                "amount": t.amount,
                "status": t.status,        # "pending", "completed", "failed"
                "reference": t.reference,
                "description": t.description,
                "date": t.timestamp,
                # Show direction clearly for the parent
                "direction": "⬆️ IN" if t.type == "topup" else "⬇️ OUT",
            }
            for t in transactions
        ]
    }


# ================================================
# PUT /wallets/{student_id}/limit
# Parent sets daily spending limit
# ================================================
@router.put("/{student_id}/limit")
def set_daily_limit(
    student_id: int,
    daily_limit: int,
    db: Session = Depends(get_db)
):
    """
    Parent sets how much their child can spend per day.
    For example: UGX 15,000 per day maximum.

    This protects against:
    - Child buying too much junk food
    - Wallet being used if bracelet is lost
    """
    # Validate the limit amount
    if daily_limit <= 0:
        raise HTTPException(
            status_code=400,
            detail="Daily limit must be greater than 0"
        )
    if daily_limit > 500_000:
        raise HTTPException(
            status_code=400,
            detail="Daily limit cannot exceed UGX 500,000"
        )

    # Find wallet
    wallet = db.query(Wallet).filter(
        Wallet.student_id == student_id
    ).first()
    if not wallet:
        raise HTTPException(
            status_code=404,
            detail=f"No wallet found for student {student_id}"
        )

    old_limit = wallet.daily_limit
    wallet.daily_limit = daily_limit
    db.commit()

    return {
        "message": "Daily spending limit updated successfully",
        "student_id": student_id,
        "old_limit": old_limit,
        "new_limit": daily_limit,
        "currency": "UGX",
    }


# ================================================
# PUT /wallets/{student_id}/deactivate
# Deactivate a wallet (e.g. bracelet lost)
# ================================================
@router.put("/{student_id}/deactivate")
def deactivate_wallet(
    student_id: int,
    db: Session = Depends(get_db)
):
    """
    Deactivate a student's wallet.
    Use this if:
    - The NFC bracelet is lost
    - The student has left the school
    - There is suspicious activity

    No payments can be made on a deactivated wallet.
    The balance and history are preserved.
    """
    wallet = db.query(Wallet).filter(
        Wallet.student_id == student_id
    ).first()
    if not wallet:
        raise HTTPException(
            status_code=404,
            detail=f"No wallet found for student {student_id}"
        )

    if not wallet.is_active:
        raise HTTPException(
            status_code=400,
            detail="Wallet is already deactivated"
        )

    wallet.is_active = False
    db.commit()

    return {
        "message": "Wallet deactivated successfully",
        "student_id": student_id,
        "wallet_id": wallet.id,
        "balance_preserved": wallet.balance,
        "note": "Contact admin to reactivate"
    }


# ================================================
# PUT /wallets/{student_id}/reactivate
# Reactivate a wallet (e.g. new bracelet issued)
# ================================================
@router.put("/{student_id}/reactivate")
def reactivate_wallet(
    student_id: int,
    db: Session = Depends(get_db)
):
    """
    Reactivate a previously deactivated wallet.
    Use this when:
    - A new NFC bracelet has been issued
    - The suspension is lifted
    """
    wallet = db.query(Wallet).filter(
        Wallet.student_id == student_id
    ).first()
    if not wallet:
        raise HTTPException(
            status_code=404,
            detail=f"No wallet found for student {student_id}"
        )

    if wallet.is_active:
        raise HTTPException(
            status_code=400,
            detail="Wallet is already active"
        )

    wallet.is_active = True
    db.commit()

    return {
        "message": "Wallet reactivated successfully",
        "student_id": student_id,
        "wallet_id": wallet.id,
        "balance": wallet.balance,
        "note": "Student can now make payments again ✅"
    }