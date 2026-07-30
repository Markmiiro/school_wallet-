from sqlalchemy import (
    Column, Integer, String, Float,
    ForeignKey, DateTime, Boolean
)
from sqlalchemy.orm import relationship
from datetime import datetime

from app.database import Base


# ════════════════════════════════════════════════
# USERS
# Parents, admins, and merchants all live here.
# Role determines what they can do.
# ════════════════════════════════════════════════
class User(Base):
    __tablename__ = "users"

    id        = Column(Integer, primary_key=True, index=True)
    name      = Column(String, nullable=False)
    phone     = Column(String, unique=True, nullable=False)
    role      = Column(String, nullable=False)   # parent | admin | merchant
    pin_hash  = Column(String, nullable=True)    # hashed PIN for login
    school_id = Column(Integer, ForeignKey("schools.id"), nullable=True)

    # Login rate-limiting (brute-force protection).
    # failed_login_attempts resets to 0 on any successful login.
    # locked_until is set once attempts hit the threshold in auth.py;
    # NULL means "not currently locked".
    failed_login_attempts = Column(Integer, default=0, nullable=False)
    locked_until           = Column(DateTime, nullable=True)

    # Relationships
    students = relationship("Student", back_populates="parent")


# ════════════════════════════════════════════════
# SCHOOLS
# ════════════════════════════════════════════════
class School(Base):
    __tablename__ = "schools"

    id        = Column(Integer, primary_key=True, index=True)
    name      = Column(String, nullable=False)
    location  = Column(String, nullable=True)
    badge_url = Column(String, nullable=True)   # school crest/logo, set via
                                                 # PUT /schools/{id}/badge or
                                                 # PUT /schools/{id}/badge-url

    # Relationships
    students  = relationship("Student", back_populates="school")
    merchants = relationship("Merchant", back_populates="school")


# ════════════════════════════════════════════════
# STUDENTS
# ════════════════════════════════════════════════
class Student(Base):
    __tablename__ = "students"

    # account_number: 12-digit parent-facing identifier, structured as
    # {3-digit school code}{9 random digits} e.g. "003482917604".
    # Generated at registration — see app/account_number.py.
    # Nullable so existing students (created before this field existed)
    # don't break; backfill separately if needed.
    #
    # dob / class_name: collected by the USSD registration flow
    # (see app/routes/ussd.py). Nullable because students created
    # through the app's Add Child screen don't supply them yet.
    #
    # dob is stored as a String, not a Date, because USSD collects it
    # as free text with no validation — forcing a Date type would break
    # on inputs like "12 Jan 2015". Normalise later if needed.
    id             = Column(Integer, primary_key=True, index=True)
    name           = Column(String, nullable=False)
    school_id      = Column(Integer, ForeignKey("schools.id"), nullable=False)
    parent_id      = Column(Integer, ForeignKey("users.id"), nullable=True)
    account_number = Column(String, unique=True, nullable=True, index=True)
    dob            = Column(String, nullable=True)   # free text, e.g. "2015-01-12"
    class_name     = Column(String, nullable=True)   # e.g. "P4", "S2"

    # Relationships
    school   = relationship("School", back_populates="students")
    parent   = relationship("User", back_populates="students")
    wallet   = relationship("Wallet", back_populates="student", uselist=False)
    # One-to-many: a student accumulates a NFCTag row per physical card
    # issued to them over time (see NFCTag below). Use
    # student.active_nfc_tag (a property, defined further down) to get
    # the one currently usable card — never assume nfc_tags[0] is it.
    nfc_tags = relationship(
        "NFCTag", back_populates="student", order_by="NFCTag.id.desc()"
    )

    @property
    def active_nfc_tag(self):
        """The one card currently usable by this student, or None.

        At most one row in nfc_tags should have is_active=True at a time
        (report-card-stolen and reissue-on-replace enforce this), but this
        takes the most recent match defensively rather than assuming it.
        """
        for tag in self.nfc_tags:
            if tag.is_active:
                return tag
        return None


# ════════════════════════════════════════════════
# WALLETS
# One wallet per student.
# Balance is in UGX (stored as Float).
# ════════════════════════════════════════════════
class Wallet(Base):
    __tablename__ = "wallets"

    id          = Column(Integer, primary_key=True, index=True)
    balance     = Column(Float, default=0.0)
    is_active   = Column(Boolean, default=True)
    daily_limit = Column(Integer, default=20000)   # UGX per day
    student_id  = Column(Integer, ForeignKey("students.id"), nullable=False)

    # Relationships
    student      = relationship("Student", back_populates="wallet")
    transactions = relationship("Transaction", back_populates="wallet")
    payments     = relationship("Payment", back_populates="wallet")


# ════════════════════════════════════════════════
# TRANSACTIONS
# Every money movement — top-ups and payments.
# NEVER delete rows from this table.
# NEVER update the amount after creation.
# ════════════════════════════════════════════════
class Transaction(Base):
    __tablename__ = "transactions"

    id          = Column(Integer, primary_key=True, index=True)
    wallet_id   = Column(Integer, ForeignKey("wallets.id"), nullable=False)
    merchant_id = Column(Integer, ForeignKey("merchants.id"), nullable=True)
    amount      = Column(Float, nullable=False)
    type        = Column(String, nullable=False)          # topup | payment
    status      = Column(String, default="pending")       # pending | completed | failed
    reference   = Column(String, nullable=True)           # Yo Uganda ExternalReference
    momo_phone  = Column(String, nullable=True)           # phone used for top-up
    description = Column(String, nullable=True)           # e.g. "Lunch money"
    timestamp   = Column(DateTime, default=datetime.utcnow)

    # Relationships
    wallet   = relationship("Wallet", back_populates="transactions")
    merchant = relationship("Merchant", back_populates="transactions")


# ════════════════════════════════════════════════
# NFC TAGS
# One row per physical card ever issued to a student — this row IS the
# physical card. tag_uid is the physical card's unique ID. A student can
# accumulate several rows over time (one-to-many, see Student.nfc_tags
# above); old rows are never deleted or overwritten, so the tag_uid of a
# lost/stolen card stays on permanent record and can never be reissued
# to anyone.
#
# is_active: whether THIS card can currently be used to spend. Checked
# by app/routes/payments.py's nfc_payment() (and the /sync offline
# path) at charge time, in addition to Wallet.is_active.
#
# status: why is_active is what it is — "active" | "stolen" | "lost" |
# "replaced" (superseded by a newer card, no theft/loss involved).
# Purely a history/audit field; is_active is what every check enforces.
#
# deactivated_at: when this row stopped being active. NULL while active.
#
# card_color: the colour the parent chose when buying the card
# (Blue | Green | Yellow | Red — the four approved by Yo Uganda
# in the USSD registration flow). Lives here rather than on
# Student because it is a property of the card, not the child.
#
# MIGRATION NOTE: status and deactivated_at are new columns. create_all()
# does not retroactively ALTER an existing table (see the Payment model's
# note below for the same caveat) — a deployed database needs, by hand:
#   ALTER TABLE nfc_tags ADD COLUMN status VARCHAR NOT NULL DEFAULT 'active';
#   ALTER TABLE nfc_tags ADD COLUMN deactivated_at TIMESTAMP;
# Not run here — no DDL against any live database from this session.
# ════════════════════════════════════════════════
class NFCTag(Base):
    __tablename__ = "nfc_tags"

    id             = Column(Integer, primary_key=True, index=True)
    tag_uid        = Column(String, unique=True, nullable=True)
    is_active      = Column(Boolean, default=True)
    status         = Column(String, nullable=False, default="active")
    deactivated_at = Column(DateTime, nullable=True)
    card_color     = Column(String, nullable=True)   # Blue | Green | Yellow | Red
    student_id     = Column(Integer, ForeignKey("students.id"), nullable=False)

    # Relationships
    student = relationship("Student", back_populates="nfc_tags")


# ════════════════════════════════════════════════
# MERCHANTS
# Tuck shop vendors inside a school.
# momo_phone receives end-of-day payout.
# ════════════════════════════════════════════════
class Merchant(Base):
    __tablename__ = "merchants"

    id         = Column(Integer, primary_key=True, index=True)
    name       = Column(String, nullable=False)
    school_id  = Column(Integer, ForeignKey("schools.id"), nullable=False)
    momo_phone = Column(String, nullable=True)
    is_active  = Column(Boolean, default=True)

    # Relationships
    school       = relationship("School", back_populates="merchants")
    transactions = relationship("Transaction", back_populates="merchant")


# ════════════════════════════════════════════════
# PAYMENTS
# Records of NFC payment attempts at tuck shop.
# Separate from Transactions to track
# payment-specific details (NFC, offline sync).
#
# reference = idempotency key: a client-generated UUID sent once per
# NFC tap by the tuck-shop device (see app/routes/payments.py's
# nfc_payment()). unique=True is defense-in-depth against a genuine
# duplicate request racing past the wallet row lock — the row lock is
# the primary protection.
#
# NOTE: unique=True only takes effect on freshly created tables
# (create_all() does not retroactively ALTER an existing table's
# constraints). The `payments` table has never had any rows written to
# it, so this is safe to create fresh anywhere it doesn't already
# exist — but on an environment where the table was already created
# without this constraint, someone needs to run
#   ALTER TABLE payments ADD CONSTRAINT payments_reference_key UNIQUE (reference);
# by hand. Not run here — no DDL against any live database from this
# session.
# ════════════════════════════════════════════════
class Payment(Base):
    __tablename__ = "payments"

    id        = Column(Integer, primary_key=True, index=True)
    wallet_id = Column(Integer, ForeignKey("wallets.id"), nullable=False)
    amount    = Column(Float, nullable=False)
    status    = Column(String, nullable=False)     # completed | failed
    reference = Column(String, nullable=True, unique=True)  # idempotency key
    timestamp = Column(DateTime, default=datetime.utcnow)

    # Relationships
    wallet = relationship("Wallet", back_populates="payments")