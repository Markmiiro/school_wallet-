# ================================================
# app/account_number.py
# ------------------------------------------------
# Generates the 12-digit parent-facing account number.
#
# Format:  {3-digit school code}{9 random digits}
#          e.g. school_id=3 -> "003482917604"
#
# This mimics the look/feel of a Ugandan bank account
# number (purely numeric, no letters/hyphens) without
# being tied to any real bank — it's entirely internal
# to School Wallet.
#
# Used by:
#   app/routes/students.py  -> create_student()
# ================================================

import random
from sqlalchemy.orm import Session

from app.models import Student

SCHOOL_CODE_DIGITS  = 3
RANDOM_DIGITS       = 9
MAX_GENERATION_TRIES = 20


def generate_account_number(db: Session, school_id: int) -> str:
    """
    Generate a unique 12-digit account number for a new student.

    Raises RuntimeError if a unique number can't be found after
    MAX_GENERATION_TRIES attempts (astronomically unlikely with
    9 random digits = up to 999,999,999 combinations per school,
    but checked defensively rather than assumed).
    """
    school_code = str(school_id).zfill(SCHOOL_CODE_DIGITS)

    for _ in range(MAX_GENERATION_TRIES):
        random_part = "".join(
            str(random.randint(0, 9)) for _ in range(RANDOM_DIGITS)
        )
        candidate = school_code + random_part

        exists = (
            db.query(Student)
            .filter(Student.account_number == candidate)
            .first()
        )
        if not exists:
            return candidate

    raise RuntimeError(
        f"Could not generate a unique account number for school {school_id} "
        f"after {MAX_GENERATION_TRIES} tries."
    )