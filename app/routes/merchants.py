from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.database import get_db
from app.models import Merchant, School, User
from app.auth import get_current_admin, get_current_user, assert_school_access, is_super_admin
from app.permissions import assert_till_staff, clean_name, clean_phone

router = APIRouter()


def merchant_payload(merchant: Merchant, viewer: User) -> dict:
    """
    momo_phone is where the merchant's sales are paid out. Only admins
    need it; till staff picking their tuck shop do not.
    """
    payload = {
        "id": merchant.id,
        "name": merchant.name,
        "school_id": merchant.school_id,
    }
    if viewer.role == "admin":
        payload["momo_phone"] = merchant.momo_phone
    return payload


# ================================================
# POST /merchants/
# Create a new merchant (tuck shop / canteen)
# Admin-only, school-scoped — a scoped admin may only create a
# merchant for their own school; a super admin may create for any.
# ================================================
@router.post("/")
def create_merchant(
    name: str,
    school_id: int,
    momo_phone: str,
    db: Session = Depends(get_db),
    current_admin: User = Depends(get_current_admin),
):
    """
    Register a tuck shop or canteen as a merchant.
    momo_phone is where their daily sales are paid out.
    """
    assert_school_access(current_admin, school_id)

    # Check school exists
    school = db.query(School).filter(School.id == school_id).first()
    if not school:
        raise HTTPException(status_code=404, detail=f"School {school_id} not found")

    merchant = Merchant(
        name=clean_name(name, what="Merchant name"),
        school_id=school_id,
        # Money is disbursed to this number — reject anything malformed
        # here rather than at payout time.
        momo_phone=clean_phone(momo_phone),
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
# List merchants. Admin only; a scoped admin sees their own school's.
# ================================================
@router.get("/")
def get_all_merchants(
    db: Session = Depends(get_db),
    current_admin: User = Depends(get_current_admin),
):
    """Merchants visible to the calling admin."""
    q = db.query(Merchant)
    if not is_super_admin(current_admin):
        q = q.filter(Merchant.school_id == current_admin.school_id)
    return [merchant_payload(m, current_admin) for m in q.all()]


# ================================================
# GET /merchants/school/{school_id}
# All merchants in a school. Staff of that school only — this is what
# the tuck shop page calls after login to pick which till it is.
#
# Declared before /{merchant_id} so "school" is never parsed as an id.
# ================================================
@router.get("/school/{school_id}")
def get_merchants_by_school(
    school_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Get all merchants (tuck shops) in a specific school."""
    assert_till_staff(current_user, school_id)

    school = db.query(School).filter(School.id == school_id).first()
    if not school:
        raise HTTPException(status_code=404, detail=f"School {school_id} not found")

    merchants = db.query(Merchant).filter(
        Merchant.school_id == school_id
    ).all()

    return {
        "school": school.name,
        "total_merchants": len(merchants),
        "merchants": [merchant_payload(m, current_user) for m in merchants],
    }


# ================================================
# GET /merchants/{merchant_id}
# Get one merchant by ID. Staff of the merchant's school only.
# ================================================
@router.get("/{merchant_id}")
def get_merchant(
    merchant_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Get a specific merchant by ID."""
    merchant = db.query(Merchant).filter(Merchant.id == merchant_id).first()
    if not merchant:
        raise HTTPException(status_code=404, detail=f"Merchant {merchant_id} not found")

    assert_till_staff(current_user, merchant.school_id)
    return merchant_payload(merchant, current_user)
