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

    print("All columns verified")


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