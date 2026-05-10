from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.database import get_db
from app.models import Merchant, School

router = APIRouter()


# ================================================
# POST /merchants/
# Create a new merchant (tuck shop / canteen)
# ================================================
@router.post("/")
def create_merchant(
    name: str,
    school_id: int,
    momo_phone: str,
    db: Session = Depends(get_db)
):
    """
    Register a tuck shop or canteen as a merchant.
    momo_phone is where their daily sales are paid out.
    """
    # Check school exists
    school = db.query(School).filter(School.id == school_id).first()
    if not school:
        raise HTTPException(status_code=404, detail=f"School {school_id} not found")

    merchant = Merchant(
        name=name,
        school_id=school_id,
        momo_phone=momo_phone,
    )
    db.add(merchant)
    db.commit()
    db.refresh(merchant)

    return {
        "message": "Merchant created successfully",
        "merchant": {
            "id": merchant.id,
            "name": merchant.name,
            "school_id": merchant.school_id,
            "momo_phone": merchant.momo_phone,
        }
    }


# ================================================
# GET /merchants/
# Get all merchants
# ================================================
@router.get("/")
def get_all_merchants(db: Session = Depends(get_db)):
    """Get all merchants in the system."""
    merchants = db.query(Merchant).all()
    return [
        {
            "id": m.id,
            "name": m.name,
            "school_id": m.school_id,
            "momo_phone": m.momo_phone,
        }
        for m in merchants
    ]


# ================================================
# GET /merchants/{merchant_id}
# Get one merchant by ID
# ================================================
@router.get("/{merchant_id}")
def get_merchant(merchant_id: int, db: Session = Depends(get_db)):
    """Get a specific merchant by ID."""
    merchant = db.query(Merchant).filter(Merchant.id == merchant_id).first()
    if not merchant:
        raise HTTPException(status_code=404, detail=f"Merchant {merchant_id} not found")

    return {
        "id": merchant.id,
        "name": merchant.name,
        "school_id": merchant.school_id,
        "momo_phone": merchant.momo_phone,
    }


# ================================================
# GET /merchants/school/{school_id}
# Get all merchants in a school
# ================================================
@router.get("/school/{school_id}")
def get_merchants_by_school(school_id: int, db: Session = Depends(get_db)):
    """Get all merchants (tuck shops) in a specific school."""
    school = db.query(School).filter(School.id == school_id).first()
    if not school:
        raise HTTPException(status_code=404, detail=f"School {school_id} not found")

    merchants = db.query(Merchant).filter(
        Merchant.school_id == school_id
    ).all()

    return {
        "school": school.name,
        "total_merchants": len(merchants),
        "merchants": [
            {"id": m.id, "name": m.name, "momo_phone": m.momo_phone}
            for m in merchants
        ]
    }