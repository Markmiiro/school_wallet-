# ================================================
# backfill_account_numbers.py
# ------------------------------------------------
# One-off maintenance script.
#
# Assigns a 12-digit account_number to any Student row that
# doesn't have one. These are students created before the
# account_number field/logic existed — see the note on the
# Student model in app/models.py.
#
# Safe to re-run: it only touches rows where account_number
# IS NULL, so running it twice does nothing the second time.
#
# Uses the same generate_account_number() as student creation,
# so the format stays consistent ({3-digit school code} +
# {9 random digits}) and uniqueness is checked per candidate.
#
# HOW TO RUN (from the app root, e.g. /app on Railway):
#     python backfill_account_numbers.py
# ================================================

from app.database import get_db
from app.models import Student
from app.account_number import generate_account_number


def main():
    # get_db() is a generator dependency; next() gives us a session.
    db = next(get_db())

    try:
        students = (
            db.query(Student)
            .filter(Student.account_number.is_(None))
            .all()
        )

        if not students:
            print("Nothing to do — every student already has an account number.")
            return

        print(f"Found {len(students)} student(s) without an account number.\n")

        updated = 0
        failed = []

        for student in students:
            try:
                number = generate_account_number(db, student.school_id)
                student.account_number = number
                # Flush per student so the next generation sees this
                # number and can't hand out a duplicate.
                db.flush()
                print(f"  id={student.id:<4} {student.name:<28} -> {number}")
                updated += 1
            except Exception as e:
                failed.append((student.id, student.name, str(e)))
                print(f"  id={student.id:<4} {student.name:<28} -> FAILED: {e}")

        db.commit()

        print(f"\nDone. {updated} student(s) updated.")
        if failed:
            print(f"{len(failed)} failed:")
            for sid, name, err in failed:
                print(f"  id={sid} {name}: {err}")

    except Exception as e:
        db.rollback()
        print(f"Backfill aborted, changes rolled back. Error: {e}")
        raise
    finally:
        db.close()


if __name__ == "__main__":
    main()