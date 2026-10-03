from typing import Optional
import re
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.database import get_db
from app.models import Student, Wallet, NFCTag, School, User, CardOrder, Transaction
from app.account_number import generate_account_number
from app.auth import (
    get_current_user,
    get_current_admin,
    is_super_admin,
    assert_school_access,
    visible_school_id,
)
from app.permissions import clean_name, clean_local_phone

router = APIRouter()


# ────────────────────────────────────────────────
# Helper — build a consistent student payload.
# Includes school name, account number, and NFC
# card status so the mobile app can display them
# without extra round trips.
# ────────────────────────────────────────────────
def student_payload(student: Student, *, for_school: bool = False) -> dict:
    nfc = student.active_nfc_tag

    if nfc is not None:
        # active_nfc_tag is by definition is_active=True, so it's either
        # a real card (tag_uid set) or the empty placeholder slot made
        # at registration (tag_uid still None).
        nfc_status = "assigned" if nfc.tag_uid else "not assigned"
        tag_uid = nfc.tag_uid
    elif student.nfc_tags:
        # No active card, but there's history — surface why (e.g. a card
        # reported "stolen" or "lost" and not yet replaced) rather than
        # lumping it in with "never had a card slot at all".
        nfc_status = student.nfc_tags[0].status
        tag_uid = None
    else:
        nfc_status = "no card slot"
        tag_uid = None

    payload = {
        "id": student.id,
        "name": student.name,
        "school_id": student.school_id,
        "school_name": student.school.name if student.school else None,
        "parent_id": student.parent_id,
        "account_number": student.account_number,
        "nfc": {
            "tag_uid": tag_uid,
            "status": nfc_status,
        },
    }
    # The roster contact is for the school office, not the till.
    if for_school:
        payload["guardian_phone"] = student.guardian_phone
    return payload


# ────────────────────────────────────────────────
# Helper — can this user look at this student?
#
#   super admin → anyone
#   admin       → students in their own school
#   merchant    → students in their own school
#   parent      → their own children only
# ────────────────────────────────────────────────
def assert_can_view_student(user: User, student: Student) -> None:
    if is_super_admin(user):
        return

    if user.role in ("admin", "merchant"):
        if user.school_id is not None and user.school_id == student.school_id:
            return

    if user.role == "parent" and student.parent_id == user.id:
        return

    raise HTTPException(status_code=403, detail="Not permitted to view this student")


# ────────────────────────────────────────────────
# Helper — canonical NFC UID form.
#
# Must match what the tuck shop page produces
#   serialNumber.replace(/:/g,'').toUpperCase()
# and what Flutter must produce
#   byte.toRadixString(16).padLeft(2,'0')  → uppercased
#
# Normalising here means a client sending "04:d1:12:ba:07:74:80"
# still lands on the same row as one sending "04D112BA077480".
# ────────────────────────────────────────────────
_UID_RE = re.compile(r"\A(?:[0-9A-F]{2}){4,10}\Z")


def normalize_uid(raw: str) -> str:
    cleaned = re.sub(r"[^0-9A-Fa-f]", "", raw or "").upper()
    if not _UID_RE.fullmatch(cleaned):
        raise HTTPException(status_code=422, detail=f"Invalid NFC UID: {raw!r}")
    return cleaned


# ================================================
# POST /students/
# Create a new student
# Auto creates wallet + NFC slot + account number
# ================================================
@router.post("/")
def create_student(
    name: str,
    school_id: int,
    parent_id: Optional[int] = None,
    guardian_phone: Optional[str] = None,
    db: Session = Depends(get_db),
    current_admin: User = Depends(get_current_admin),
):
    """Register a new student. Automatically creates their wallet,
    an empty NFC tag slot (filled in later via /assign-nfc), and a
    parent-facing account number.

    A roster entry usually has no parent yet, only guardian_phone: the
    parent gets the child by verifying that number (app/routes/family.py).
    """

    # A scoped admin can only create students in their own school
    assert_school_access(current_admin, school_id)

    name = clean_name(name, what="Student name")

    # Check school exists
    school = db.query(School).filter(School.id == school_id).first()
    if not school:
        raise HTTPException(status_code=404, detail=f"School {school_id} not found")

    # Check parent exists
    if parent_id is not None:
        parent = db.query(User).filter(User.id == parent_id).first()
        if not parent:
            raise HTTPException(status_code=404, detail=f"Parent {parent_id} not found")

    phone = clean_local_phone(guardian_phone) if guardian_phone else None

    # Create student
    student = Student(
        name=name,
        school_id=school_id,
        parent_id=parent_id,
        guardian_phone=phone,
    )
    db.add(student)
    db.flush()  # get student.id before committing

    # Generate the parent-facing account number now that we have student.id
    student.account_number = generate_account_number(db, school_id)

    # Auto create wallet — starts at zero balance
    wallet = Wallet(
        student_id=student.id,
        balance=0,
        is_active=True
    )
    db.add(wallet)

    # Auto create NFC slot — no bracelet assigned yet
    nfc_tag = NFCTag(
        student_id=student.id,
        tag_uid=None,
    )
    db.add(nfc_tag)

    # Save everything at once
    db.commit()
    db.refresh(student)

    return {
        "message": "Student created successfully",
        "student": {
            "id": student.id,
            "name": student.name,
            "school_id": student.school_id,
            "school_name": school.name,
            "parent_id": student.parent_id,
            "guardian_phone": student.guardian_phone,
            "account_number": student.account_number,
        },
        "wallet": {
            "id": wallet.id,
            "balance": wallet.balance,
            "is_active": wallet.is_active,
            "daily_limit": wallet.daily_limit,
        },
        "nfc_tag": {
            "tag_uid": nfc_tag.tag_uid,
            "status": "not assigned"
        }
    }


# ================================================
# GET /students/
# Students visible to the caller.
#
# Was: every student in the system, to anyone with the URL.
# Now scoped by role — this is also what stops a parent's token
# returning every child in the country.
# ================================================
@router.get("/")
def get_all_students(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Students visible to the caller, scoped by role."""
    q = db.query(Student)

    if current_user.role in ("admin", "merchant"):
        school = visible_school_id(current_user)   # None only for super admin
        if school is not None:
            q = q.filter(Student.school_id == school)

    elif current_user.role == "parent":
        q = q.filter(Student.parent_id == current_user.id)

    else:
        raise HTTPException(status_code=403, detail="Not permitted")

    for_school = current_user.role == "admin"
    return [student_payload(s, for_school=for_school) for s in q.all()]


# ================================================
# GET /students/{student_id}
# Get ONE student by their ID
# Includes school name, account number, and NFC status.
# ================================================
@router.get("/{student_id}")
def get_student(
    student_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Get a specific student by their ID."""
    student = db.query(Student).filter(Student.id == student_id).first()
    if not student:
        raise HTTPException(
            status_code=404,
            detail=f"Student with ID {student_id} not found"
        )

    assert_can_view_student(current_user, student)
    return student_payload(student)


# ================================================
# GET /students/school/{school_id}
# Get all students in a specific school
# ================================================
@router.get("/school/{school_id}")
def get_students_by_school(
    school_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Get all students enrolled in a specific school."""
    # Only staff of that school (or a super admin) may list it
    if current_user.role not in ("admin", "merchant"):
        raise HTTPException(status_code=403, detail="Not permitted")
    assert_school_access(current_user, school_id)

    school = db.query(School).filter(School.id == school_id).first()
    if not school:
        raise HTTPException(status_code=404, detail=f"School {school_id} not found")

    students = db.query(Student).filter(Student.school_id == school_id).all()
    return {
        "school": school.name,
        "total_students": len(students),
        "students": [student_payload(s) for s in students]
    }


# ================================================
# GET /students/parent/{parent_id}
# Get all students under one parent
# ================================================
@router.get("/parent/{parent_id}")
def get_students_by_parent(
    parent_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Get all students belonging to a specific parent."""
    # A parent may only ask about themselves
    if current_user.role == "parent" and current_user.id != parent_id:
        raise HTTPException(status_code=403, detail="Not permitted")
    if current_user.role not in ("parent", "admin"):
        raise HTTPException(status_code=403, detail="Not permitted")

    parent = db.query(User).filter(User.id == parent_id).first()
    if not parent:
        raise HTTPException(status_code=404, detail=f"Parent {parent_id} not found")

    q = db.query(Student).filter(Student.parent_id == parent_id)

    # A scoped admin sees only the children at their own school
    if current_user.role == "admin" and not is_super_admin(current_user):
        q = q.filter(Student.school_id == current_user.school_id)

    students = q.all()
    return {
        "parent": parent.name,
        "total_children": len(students),
        "students": [student_payload(s) for s in students]
    }


# ================================================
# PUT /students/{student_id}/assign-nfc
# Link a physical NFC card to a student, by the card's number.
#
# tag_uid is the card number: the card's UID in hex, either read by
# tapping (the school's /issue/ page) or typed in by hand (the parent
# app, and the /issue/ page's manual entry).
#
# Callable by an admin scoped to the student's school (or a super
# admin) — may link a first card, or REPLACE a working one.
#
# Parents may not (decision 3 Oct 2026). A card number is printed on the
# card and readable by any NFC phone, so it proves nothing about who is
# asking; anyone who handled a card could have linked it. The school
# links each card from its roster at handout.
#
# Nobody creates students here. The student must already exist.
# ================================================
@router.put("/{student_id}/assign-nfc")
def assign_nfc_tag(
    student_id: int,
    tag_uid: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """
    Link a physical NFC card to a student.
    Once linked, the student can tap to pay at the tuck shop.
    """
    # Check student exists
    student = db.query(Student).filter(Student.id == student_id).first()
    if not student:
        raise HTTPException(status_code=404, detail="Student not found")

    if current_user.role == "admin":
        # A Seeta admin cannot issue cards at Kampala Parents.
        # Note the school comes from the STUDENT, never from the request.
        assert_school_access(current_user, student.school_id)
    elif current_user.role == "parent":
        raise HTTPException(
            status_code=403,
            detail="Cards are linked by the school. Ask the school office "
                   "to link your child's card.",
        )
    else:
        raise HTTPException(status_code=403, detail="Not permitted")

    uid = normalize_uid(tag_uid)

    # A tag_uid is a physical card's permanent identity. Once any row
    # anywhere carries it — active, replaced, lost, or stolen — it can
    # never be (re)assigned again, even back to the same student. This
    # is what keeps a reported-stolen card permanently unusable.
    already_used = db.query(NFCTag).filter(NFCTag.tag_uid == uid).first()
    if already_used:
        raise HTTPException(
            status_code=400,
            detail=(
                f"Card {uid} is already linked or was retired, and cannot be "
                f"used again. Check the number, or ask the school."
            ),
        )

    active = student.active_nfc_tag
    now = datetime.utcnow()

    if active is None:
        # No usable card right now — either this student has no nfc_tags
        # row at all (shouldn't happen post-registration, but be safe),
        # or their last card was reported stolen/lost. Either way, a
        # brand new row for the new physical card.
        nfc = NFCTag(student_id=student_id, tag_uid=uid, is_active=True,
                     status="active", linked_at=now)
        db.add(nfc)
    elif active.tag_uid is None:
        # Empty placeholder from registration — never represented a real
        # physical card, so fill it in place rather than spawning history.
        active.tag_uid = uid
        active.status = "active"
        active.linked_at = now
    else:
        # Swapping a working card for a new one (not a theft/loss report —
        # see POST /students/{id}/report-stolen for that). Retire the old
        # row and start a fresh one so the old tag_uid stays on record.
        active.is_active = False
        active.status = "replaced"
        active.deactivated_at = now
        nfc = NFCTag(student_id=student_id, tag_uid=uid, is_active=True,
                     status="active", linked_at=now)
        db.add(nfc)

    # A card bought in the app (app/routes/cards.py) has now been handed
    # over: close the order so the school's list of cards owed is right
    # and the parent can buy again if this card is ever lost.
    db.query(CardOrder).filter(
        CardOrder.student_id == student_id, CardOrder.status == "paid",
    ).update(
        {"status": "fulfilled", "fulfilled_at": now},
        synchronize_session=False,
    )

    db.commit()

    return {
        "message": "NFC card assigned successfully",
        "student_id": student_id,
        "student_name": student.name,
        "tag_uid": uid,
        "status": "assigned"
    }


# ================================================
# PUT /students/{student_id}/deactivate
# Deactivate a student who left the school
# ================================================
# ================================================
# POST /students/{student_id}/undo-card-link
# Free a card the school linked to the wrong child, so it can go to the
# right one. Only while nothing has been bought since the link: after
# that the card has spent someone's money and must be reported or
# replaced instead, which keeps its number on record for good.
# ================================================
@router.post("/{student_id}/undo-card-link")
def undo_card_link(
    student_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    student = db.query(Student).filter(Student.id == student_id).first()
    if not student:
        raise HTTPException(status_code=404, detail="Student not found")
    if current_user.role != "admin":
        raise HTTPException(status_code=403, detail="Not permitted")
    assert_school_access(current_user, student.school_id)

    card = student.active_nfc_tag
    if card is None or card.tag_uid is None:
        raise HTTPException(status_code=404, detail="This child has no linked card.")
    if card.linked_at is None:
        raise HTTPException(
            status_code=409,
            detail="This card was linked before undo was possible. Report it "
                   "lost or replace it instead.",
        )

    wallet = student.wallet
    spent = wallet is not None and db.query(Transaction).filter(
        Transaction.wallet_id == wallet.id,
        Transaction.type == "payment",
        Transaction.status == "completed",
        Transaction.timestamp >= card.linked_at,
    ).first() is not None
    if spent:
        raise HTTPException(
            status_code=409,
            detail="Something has been bought with this card since it was "
                   "linked. Report it lost or replace it instead.",
        )

    uid, linked_at = card.tag_uid, card.linked_at
    # Back to the empty slot registration makes; no row keeps the number,
    # so it can be linked again.
    card.tag_uid = None
    card.linked_at = None
    card.status = "active"

    # A card order this link closed is owed again.
    db.query(CardOrder).filter(
        CardOrder.student_id == student_id,
        CardOrder.status == "fulfilled",
        CardOrder.fulfilled_at >= linked_at,
    ).update({"status": "paid", "fulfilled_at": None}, synchronize_session=False)

    db.commit()
    return {"message": f"Card {uid} unlinked from {student.name}.", "tag_uid": uid}


# ================================================
# PUT /students/{student_id}/guardian-phone?phone=
# Set or correct the roster's guardian phone. An empty phone clears it.
# Does not move a child already on a parent's account: that stays a
# deliberate school decision.
# ================================================
@router.put("/{student_id}/guardian-phone")
def set_guardian_phone(
    student_id: int,
    phone: str = "",
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    student = db.query(Student).filter(Student.id == student_id).first()
    if not student:
        raise HTTPException(status_code=404, detail="Student not found")
    if current_user.role != "admin":
        raise HTTPException(status_code=403, detail="Not permitted")
    assert_school_access(current_user, student.school_id)

    student.guardian_phone = clean_local_phone(phone) if phone.strip() else None
    db.commit()
    return {"student_id": student.id, "guardian_phone": student.guardian_phone}


@router.put("/{student_id}/deactivate")
def deactivate_student(
    student_id: int,
    db: Session = Depends(get_db),
    current_admin: User = Depends(get_current_admin),
):
    """
    Deactivate a student.
    Never deletes — keeps full transaction history intact.
    Also deactivates their wallet and card so no payments can be made.
    """
    student = db.query(Student).filter(Student.id == student_id).first()
    if not student:
        raise HTTPException(status_code=404, detail="Student not found")

    assert_school_access(current_admin, student.school_id)

    # Deactivate wallet too
    wallet = db.query(Wallet).filter(Wallet.student_id == student_id).first()
    if wallet:
        wallet.is_active = False

    # And the card — a child who left should not have a live card in a drawer
    nfc = student.active_nfc_tag
    if nfc:
        nfc.is_active = False
        nfc.deactivated_at = datetime.utcnow()

    db.commit()

    return {
        "message": f"{student.name} has been deactivated",
        "student_id": student_id,
        "wallet_deactivated": True,
        "card_deactivated": bool(nfc),
        "note": "Transaction history is preserved"
    }


# ================================================
# POST /students/{student_id}/report-stolen
# Immediately deactivate a student's current card.
#
# Callable by:
#   - an admin scoped to the student's school (or a super admin)
#   - the student's own parent
# The wallet and balance are untouched — this only blocks the physical
# card. Issue a replacement afterwards via PUT .../assign-nfc, which
# (per the one-to-many NFCTag model) creates a fresh card row rather
# than reviving this one; this tag_uid can never be reassigned.
# ================================================
@router.post("/{student_id}/report-stolen")
def report_card_stolen(
    student_id: int,
    reason: str = "stolen",
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Deactivate a student's current NFC card because it was lost or stolen."""
    if reason not in ("stolen", "lost"):
        raise HTTPException(status_code=422, detail='reason must be "stolen" or "lost"')

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

    nfc = student.active_nfc_tag
    if nfc is None or nfc.tag_uid is None:
        raise HTTPException(status_code=400, detail=f"{student.name} has no active card to report")

    nfc.is_active = False
    nfc.status = reason
    nfc.deactivated_at = datetime.utcnow()
    db.commit()

    return {
        "message": f"Card {nfc.tag_uid} reported {reason} and deactivated",
        "student_id": student_id,
        "student_name": student.name,
        "tag_uid": nfc.tag_uid,
        "status": reason,
        "wallet_untouched": True,
    }