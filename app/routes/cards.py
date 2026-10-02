# ================================================
# routes/cards.py
# ------------------------------------------------
# Buying a Smart Card from the parent app, for a child who already
# exists. (The USSD flow in ussd.py registers a NEW child and sells the
# card in one step; parents do not create children in the app.)
#
# FLOW:
# 1. Parent picks a child, a colour, and the phone that will pay
# 2. Server records a pending CardOrder, then asks Yo Uganda to charge
# 3. Parent approves the prompt with their MoMo PIN
# 4. The app polls GET /cards/orders/{reference}; the server asks Yo
#    for the transaction's status and marks the order paid or failed
# 5. The school hands over a card of that colour and links it — linking
#    (PUT /students/{id}/assign-nfc) marks the order fulfilled
#
# WHY POLLING, NOT THE WEBHOOK: webhook.py credits the wallet for any
# pending Transaction matching the reference Yo confirms. A card fee is
# not a top-up, so it lives in card_orders, where the webhook finds
# nothing for a CARD-… reference and does nothing. Confirmation
# therefore comes only from the status check below — which also runs
# whenever a child's or a school's orders are listed, so a parent who
# closes the app before step 4 finishes is still picked up.
# ================================================

from datetime import datetime, timedelta
from typing import Optional
import uuid

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, field_validator
from sqlalchemy.orm import Session

from app.auth import (
    assert_school_access, get_current_admin, get_current_user, visible_school_id,
)
from app.database import get_db
from app.models import CardOrder, NFCTag, Student, User
from app.momo import charge_mobile_money, verify_transaction

router = APIRouter()

# Fixed price of a Smart Card and the four colours, as in the USSD flow
# approved by Yo Uganda (REGISTRATION_FEE / CARD_COLORS in ussd.py).
CARD_PRICE_UGX = 25_000
CARD_COLORS = ("Blue", "Green", "Yellow", "Red")

# A pending order older than this is no longer re-checked with Yo when
# orders are listed. Yo resolves a charge within about an hour.
_RECHECK_WINDOW = timedelta(hours=24)


# ================================================
# SCHEMAS
# ================================================

class CardOrderRequest(BaseModel):
    student_id: int
    card_color: str
    phone_number: str
    network: str

    @field_validator("card_color")
    def color_must_be_valid(cls, v):
        v = v.strip().capitalize()
        if v not in CARD_COLORS:
            raise ValueError("Card colour must be Blue, Green, Yellow or Red")
        return v

    @field_validator("phone_number")
    def phone_must_be_valid(cls, v):
        v = v.replace(" ", "").replace("+", "")
        if not v.startswith("256"):
            raise ValueError("Phone must start with 256. Example: 256771234567")
        if not v.isdigit():
            raise ValueError("Phone must contain digits only")
        if len(v) != 12:
            raise ValueError(f"Phone must be 12 digits. Got {len(v)}: {v}")
        return v

    @field_validator("network")
    def network_must_be_valid(cls, v):
        v = v.upper().strip()
        if v not in ["MTN", "AIRTEL"]:
            raise ValueError("Network must be MTN or AIRTEL")
        return v


_STATUS_MESSAGES = {
    "pending":   "Waiting for approval on the phone",
    "paid":      "Card paid for. Collect it from the school.",
    "failed":    "Payment failed or was rejected",
    "fulfilled": "Card paid for and linked",
}


def _order_payload(order: CardOrder) -> dict:
    return {
        "reference_id": order.reference,
        "student_id":   order.student_id,
        "student_name": order.student.name if order.student else None,
        "card_color":   order.card_color,
        "amount":       order.amount,
        "status":       order.status,
        "message":      _STATUS_MESSAGES.get(order.status, "Unknown status"),
        "phone":        order.momo_phone,
        "network":      order.network,
        "date":         order.created_at,
        "paid_at":      order.paid_at,
    }


# ────────────────────────────────────────────────
# Who may buy, or look at, a card order for this child?
#   admin  → children in their own school (super admin: any)
#   parent → their own children only
# Tuck-shop staff have no business with card orders.
# ────────────────────────────────────────────────
def _assert_can_order_for(user: User, student: Optional[Student]) -> None:
    if student is None:
        raise HTTPException(status_code=404, detail="Student not found")
    if user.role == "admin":
        assert_school_access(user, student.school_id)
        return
    if user.role == "parent" and student.parent_id == user.id:
        return
    raise HTTPException(status_code=403, detail="Not permitted for this child")


def _reserve_card_slot(db: Session, student: Student, card_color: str) -> None:
    """
    Record the paid-for colour on the child's card slot, as USSD
    registration does: an active NFCTag row with no tag_uid yet. The
    school fills in the tag_uid when it links the physical card.
    """
    slot = student.active_nfc_tag
    if slot is None:
        db.add(NFCTag(
            student_id=student.id, tag_uid=None, is_active=True,
            status="active", card_color=card_color,
        ))
    elif slot.tag_uid is None:
        slot.card_color = card_color
    # A working card appeared while the payment was in flight: leave it
    # alone. The order still shows as paid, for the school to settle.


async def _refresh_from_yo(db: Session, order: CardOrder) -> CardOrder:
    """
    Ask Yo Uganda what happened to a pending order and record the answer.
    Anything other than an explicit SUCCEEDED or FAILED leaves it pending.
    """
    if order.status != "pending":
        return order

    reference = order.reference
    try:
        yo_status = await verify_transaction(reference)
    except Exception as e:
        print(f"⚠️  Could not poll Yo Uganda for {reference}: {e}")
        return order

    latest_status = yo_status.get("TransactionStatus", "INDETERMINATE")
    if latest_status not in ("SUCCEEDED", "FAILED"):
        return order

    # `order` was loaded before the await, so it is stale by now. Re-read
    # it under a row lock so two polls landing together cannot both act
    # on it; populate_existing() because the object is already in this
    # session's identity map. Same pattern as check_topup_status().
    order = (
        db.query(CardOrder)
        .filter(CardOrder.reference == reference)
        .populate_existing()
        .with_for_update()
        .first()
    )
    if order.status != "pending":
        db.commit()  # release the lock
        return order

    if latest_status == "SUCCEEDED":
        order.status = "paid"
        order.paid_at = datetime.utcnow()
        student = db.query(Student).filter(Student.id == order.student_id).first()
        if student:
            _reserve_card_slot(db, student, order.card_color)
        print(f"✅ Card order paid: {reference}")
    else:
        order.status = "failed"

    db.commit()
    return order


async def _refresh_recent_pending(db: Session, orders: list) -> None:
    cutoff = datetime.utcnow() - _RECHECK_WINDOW
    for order in orders:
        if order.status == "pending" and order.created_at and order.created_at >= cutoff:
            await _refresh_from_yo(db, order)


# ================================================
# ENDPOINT 1 — Buy a card for a child
# ================================================
@router.post("/orders")
async def buy_card(
    order_data: CardOrderRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """
    Charge the given phone UGX 25,000 for a Smart Card for this child.
    The price is fixed here; the caller does not send an amount.
    """
    student = db.query(Student).filter(Student.id == order_data.student_id).first()
    _assert_can_order_for(current_user, student)

    working = student.active_nfc_tag
    if working is not None and working.tag_uid is not None:
        raise HTTPException(
            status_code=409,
            detail=(
                f"{student.name} already has a working card. Report it lost "
                f"or stolen first if it needs replacing."
            ),
        )

    # Settle any earlier attempt before deciding whether this is a
    # second payment for the same card.
    earlier = (
        db.query(CardOrder)
        .filter(CardOrder.student_id == student.id, CardOrder.status.in_(["pending", "paid"]))
        .all()
    )
    await _refresh_recent_pending(db, earlier)
    if any(o.status == "paid" for o in earlier):
        raise HTTPException(
            status_code=409,
            detail=(
                f"A card for {student.name} is already paid for. Collect it "
                f"from the school."
            ),
        )

    # Record the order BEFORE charging, so a charge can never exist
    # without a row that says what it was for.
    student_name = student.name
    order = CardOrder(
        student_id=student.id,
        ordered_by=current_user.id,
        card_color=order_data.card_color,
        amount=CARD_PRICE_UGX,
        status="pending",
        reference=f"CARD-{uuid.uuid4()}",
        momo_phone=order_data.phone_number,
        network=order_data.network,
    )
    db.add(order)
    db.commit()
    db.refresh(order)

    yo_response = await charge_mobile_money(
        phone=order.momo_phone,
        amount=order.amount,
        network=order.network,
        tx_ref=order.reference,
        customer_name=f"{student_name} (Smart Card)",
    )

    if yo_response.get("Status") != "OK":
        order.status = "failed"
        db.commit()
        raise HTTPException(
            status_code=400,
            detail=(
                f"Payment initiation failed: "
                f"{yo_response.get('StatusMessage', 'Unknown error from Yo Uganda')}"
            ),
        )

    print(f"\n💳 Card order initiated via Yo Uganda:")
    print(f"   Student:  {student_name}")
    print(f"   Colour:   {order.card_color}")
    print(f"   Our Ref:  {order.reference}")

    payload = _order_payload(order)
    payload["message"] = (
        f"Payment request sent to {order.momo_phone}. "
        f"Enter your {order.network} PIN to approve."
    )
    return payload


# ================================================
# ENDPOINT 2 — A child's card orders, newest first
# ================================================
@router.get("/orders/student/{student_id}")
async def student_card_orders(
    student_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    student = db.query(Student).filter(Student.id == student_id).first()
    _assert_can_order_for(current_user, student)

    orders = (
        db.query(CardOrder)
        .filter(CardOrder.student_id == student_id)
        .order_by(CardOrder.id.desc())
        .limit(20)
        .all()
    )
    await _refresh_recent_pending(db, orders)
    return {"student_id": student_id, "orders": [_order_payload(o) for o in orders]}


# ================================================
# ENDPOINT 3 — A school's card orders, for handing cards over
# ================================================
@router.get("/orders/school/{school_id}")
async def school_card_orders(
    school_id: int,
    status: str = Query(default="paid", description="pending | paid | failed | fulfilled"),
    limit: int = Query(default=50, ge=1, le=100),
    db: Session = Depends(get_db),
    current_admin: User = Depends(get_current_admin),
):
    assert_school_access(current_admin, school_id)

    in_school = (
        db.query(CardOrder)
        .join(Student, Student.id == CardOrder.student_id)
        .filter(Student.school_id == school_id)
    )

    # Pick up payments whose parent closed the app before it confirmed.
    pending = (
        in_school.filter(CardOrder.status == "pending")
        .order_by(CardOrder.id.desc())
        .limit(20)
        .all()
    )
    await _refresh_recent_pending(db, pending)

    orders = (
        in_school.filter(CardOrder.status == status)
        .order_by(CardOrder.id.desc())
        .limit(limit)
        .all()
    )
    return {
        "school_id": school_id,
        "status": status,
        "orders": [_order_payload(o) for o in orders],
    }


# ================================================
# ENDPOINT 4 — Cards paid for and not yet handed over
#
# What the /issue/ page shows. Scoped by the caller, not by a school id
# in the URL: a school admin sees their own school, a super admin all.
# ================================================
@router.get("/orders/owed")
async def cards_owed(
    db: Session = Depends(get_db),
    current_admin: User = Depends(get_current_admin),
):
    visible = db.query(CardOrder).join(Student, Student.id == CardOrder.student_id)
    school = visible_school_id(current_admin)   # None only for super admin
    if school is not None:
        visible = visible.filter(Student.school_id == school)

    # Pick up payments whose parent closed the app before it confirmed.
    pending = (
        visible.filter(CardOrder.status == "pending")
        .order_by(CardOrder.id.desc())
        .limit(20)
        .all()
    )
    await _refresh_recent_pending(db, pending)

    orders = (
        visible.filter(CardOrder.status == "paid")
        .order_by(CardOrder.id.asc())
        .limit(500)
        .all()
    )
    return {"orders": [_order_payload(o) for o in orders]}


# ================================================
# ENDPOINT 5 — Check one order (the app polls this)
# ================================================
@router.get("/orders/{reference_id}")
async def check_card_order(
    reference_id: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """
    - pending → not approved yet
    - paid    → money confirmed; collect the card from the school
    - failed  → rejected or timed out
    """
    order = db.query(CardOrder).filter(CardOrder.reference == reference_id).first()
    if not order:
        raise HTTPException(status_code=404, detail=f"No card order found: {reference_id}")

    student = db.query(Student).filter(Student.id == order.student_id).first()
    _assert_can_order_for(current_user, student)

    order = await _refresh_from_yo(db, order)
    return _order_payload(order)
