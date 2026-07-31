# ================================================
# database.py — PostgreSQL version
# ------------------------------------------------
# Now using PostgreSQL instead of SQLite.
#
# WHY PostgreSQL?
# - Handles many users at once
# - Production ready
# - Required for deployment
# - Better performance
# - More reliable for financial data
# ================================================

from sqlalchemy import create_engine, text, inspect
from sqlalchemy.orm import sessionmaker, declarative_base
from dotenv import load_dotenv
import os

load_dotenv()

DATABASE_URL = os.getenv("DATABASE_URL")

if not DATABASE_URL:
    raise ValueError(
        "DATABASE_URL is not set in .env\n"
        "Example: postgresql://user:password@localhost/schoolwallet"
    )

# ── Create engine ────────────────────────────────
# PostgreSQL does not need check_same_thread
# pool_size → keep 5 connections ready
# max_overflow → allow 10 extra during busy periods
engine = create_engine(
    DATABASE_URL,
    pool_size=5,
    max_overflow=10,
    pool_timeout=30,
    pool_recycle=1800,
    echo=False
)

SessionLocal = sessionmaker(
    autocommit=False,
    autoflush=False,
    bind=engine
)

Base = declarative_base()


def get_db():
    db = SessionLocal()
    try:
        yield db
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


def test_connection():
    try:
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
        print("PostgreSQL connected successfully")
    except Exception as e:
        print(f"Database connection failed: {e}")
        raise


def create_tables():
    from app import models  # noqa
    Base.metadata.create_all(bind=engine)
    print("All tables created in PostgreSQL")

    # Auto-add missing columns for tables that already existed before
    # the model gained a new field. Each column is handled in its own
    # transaction — see add_column_if_missing() for why that matters.
    add_column_if_missing("merchants",    "is_active",   "BOOLEAN DEFAULT TRUE")
    add_column_if_missing("wallets",      "daily_limit", "INTEGER DEFAULT 20000")
    add_column_if_missing("transactions", "status",      "VARCHAR DEFAULT 'pending'")
    add_column_if_missing("transactions", "reference",   "VARCHAR")
    add_column_if_missing("transactions", "momo_phone",  "VARCHAR")
    add_column_if_missing("transactions", "description", "VARCHAR")
    add_column_if_missing("users",        "pin_hash",    "VARCHAR")
    add_column_if_missing("users",        "school_id",   "INTEGER")

    # Student registration fields collected by the USSD flow.
    # Nullable — students added via the app don't supply them yet.
    add_column_if_missing("students",     "dob",         "VARCHAR")
    add_column_if_missing("students",     "class_name",  "VARCHAR")

    # Card colour chosen at purchase (Blue | Green | Yellow | Red).
    # Lives on the card record, not the student.
    add_column_if_missing("nfc_tags",     "card_color",  "VARCHAR")

    # Lost/stolen-card support: NFCTag went from one-to-one with Student
    # to one-to-many (see app/models.py). status/deactivated_at record
    # why and when a given physical card stopped being usable.
    add_column_if_missing("nfc_tags",     "status",         "VARCHAR NOT NULL DEFAULT 'active'")
    add_column_if_missing("nfc_tags",     "deactivated_at", "TIMESTAMP")

    print("All columns verified")

    # Closes the webhook double-credit race (app/routes/webhook.py):
    # a repeated Yo IPN must not be able to insert a second Transaction
    # with the same reference. Production was audited for pre-existing
    # duplicate references before this was added (none found) — see
    # add_unique_constraint_if_missing()'s docstring for why a failure
    # here is logged loudly rather than swallowed.
    add_unique_constraint_if_missing("transactions", "reference", "uq_transactions_reference")

    # payments.py's nfc_payment()/sync_offline_payments() idempotency
    # backstop (behind the wallet row lock) depends on this constraint —
    # see the Payment model's comment in app/models.py. Audit production
    # for pre-existing duplicate references before this runs against it
    # for the first time (see the duplicate-audit query given alongside
    # this change) — same reasoning as the transactions.reference case
    # above.
    add_unique_constraint_if_missing("payments", "reference", "uq_payments_reference")


def add_column_if_missing(table: str, column: str, col_type: str):
    """
    Adds a column only if it does not already exist.

    IMPORTANT — why this checks first instead of try/except:

    On PostgreSQL, a failed statement ABORTS the whole transaction.
    An earlier version ran every ALTER inside one shared transaction and
    swallowed failures with a bare `except: pass`. That meant the first
    already-existing column aborted the transaction, and every column
    after it silently failed too — so new columns often never got added
    and no error was ever printed.

    This version:
      1. Inspects the schema to see if the column is really missing.
      2. Runs each ALTER in its own short transaction, so one failure
         can't poison the others.
      3. Prints real errors instead of hiding them.
    """
    try:
        inspector = inspect(engine)

        # If the table doesn't exist yet, create_all() will have made it
        # with all current model columns — nothing to patch.
        if table not in inspector.get_table_names():
            return

        existing = {col["name"] for col in inspector.get_columns(table)}
        if column in existing:
            return  # Already there — nothing to do.

        # Own transaction per column so a failure can't cascade.
        with engine.begin() as conn:
            conn.execute(
                text(f"ALTER TABLE {table} ADD COLUMN {column} {col_type}")
            )
        print(f"   Added column: {table}.{column}")

    except Exception as e:
        # Log loudly rather than silently swallowing — a genuinely
        # failed migration should be visible in the deploy logs.
        print(f"   WARNING: could not add {table}.{column}: {e}")


def add_unique_constraint_if_missing(table: str, column: str, constraint_name: str):
    """
    Adds a single-column UNIQUE constraint only if it does not already
    exist. Same reasoning as add_column_if_missing(): check first, own
    transaction so a failure can't cascade, log loudly instead of
    swallowing — if this ever fails on a live deploy (e.g. a duplicate
    slipped in between the audit and the deploy), that must be visible,
    not silent, since it means the double-credit race this constraint
    exists to close is still open.

    Postgres treats NULLs as distinct under UNIQUE, so this is safe to
    add on a nullable column — multiple NULL references can still coexist.
    """
    try:
        inspector = inspect(engine)

        if table not in inspector.get_table_names():
            return

        existing = {
            tuple(uc["column_names"]) for uc in inspector.get_unique_constraints(table)
        }
        if (column,) in existing:
            return  # Already there — nothing to do.

        with engine.begin() as conn:
            conn.execute(
                text(f"ALTER TABLE {table} ADD CONSTRAINT {constraint_name} UNIQUE ({column})")
            )
        print(f"   Added unique constraint: {table}.{column} ({constraint_name})")

    except Exception as e:
        print(f"   WARNING: could not add unique constraint {constraint_name} on {table}.{column}: {e}")