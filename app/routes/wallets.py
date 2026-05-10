from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.database import get_db
from app.models import Wallet, Transaction,Student

router = APIRouter()


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
# ==========================================

@router.get("/wallets/{student_id}")
def get_wallet(
    student_id: int,
    db: Session = Depends(get_db)
):

    # STEP 1 → confirm student exists
    student = db.query(Student).filter(Student.id == student_id).first()

    if not student:
        raise HTTPException(
            status_code=404,
            detail="Student not found"
        )

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
        "is_active": wallet.is_active
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
    Shows both top-ups (money IN) and payments (money OUT).
    Ordered newest first.
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
                "direction": "⬆️ IN" if t.type == "topup" else "⬇️ OUT",
                "amount": t.amount,
                "status": t.status,
                "reference": t.reference,
                "description": t.description,
                "date": t.timestamp,
            }
            for t in transactions
        ]
    }