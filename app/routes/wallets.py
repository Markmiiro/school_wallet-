from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.database import get_db
from app.models import Wallet, Student

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