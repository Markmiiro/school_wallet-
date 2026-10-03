# ================================================
# app/controls.py
# ------------------------------------------------
# Shared pieces of a child's spending controls: the card's state as a
# parent sees it, and the audit record every change must write
# (models.ControlChange). Routes: app/routes/wallets.py (limit, controls
# view) and app/routes/students.py (block, unblock, lost/stolen).
# ================================================

from sqlalchemy.orm import Session

from app.models import ControlChange, NFCTag, Student, User


def record_change(db: Session, student: Student, actor: User, control: str,
                  old, new) -> None:
    """Add an audit row to the caller's transaction. No row if nothing changed."""
    if str(old) == str(new):
        return
    db.add(ControlChange(
        student_id=student.id, actor_user_id=actor.id, actor_role=actor.role,
        control=control,
        old_value=None if old is None else str(old),
        new_value=None if new is None else str(new),
    ))


def blocked_card(student: Student):
    for tag in student.nfc_tags:
        if tag.status == "blocked":
            return tag
    return None


def card_state(student: Student) -> tuple:
    """
    (state, card row). state is one of:
      active   a working card
      blocked  paused by the parent or school; can be unblocked
      lost | stolen | replaced | closed   the last card, now retired
      none     no card was ever linked
    """
    active = student.active_nfc_tag
    if active is not None and active.tag_uid:
        return "active", active
    blocked = blocked_card(student)
    if blocked is not None:
        return "blocked", blocked
    for tag in student.nfc_tags:          # newest first
        if tag.tag_uid:
            return tag.status or "retired", tag
    return "none", None
