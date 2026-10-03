from sqlalchemy import (
    Column, Integer, String, Date,
    ForeignKey, DateTime, Boolean, UniqueConstraint, Text
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

    # Terms and privacy acceptance: WHICH version this user agreed to and
    # WHEN (UTC). Both NULL means "has not accepted". Compared against
    # CURRENT_TERMS_VERSION in app/terms.py at signup and login.
    #
    # MIGRATION NOTE: these are NOT in create_tables()' self-heal list on
    # purpose. Apply migrations/2026_10_02_add_terms_acceptance.sql by
    # hand before deploying; every query on users selects them.
    terms_version     = Column(String, nullable=True)
    terms_accepted_at = Column(DateTime, nullable=True)

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
    # From the school's roster: the guardian's phone, 256XXXXXXXXX. A
    # parent whose verified account phone matches gets this child (see
    # app/routes/family.py). Not the same as parent_id: the roster can
    # name a guardian who has not signed up yet.
    guardian_phone = Column(String, nullable=True, index=True)

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
# Balance is in UGX (stored as Integer — UGX has no subunit, and
# storing money as Float risks binary floating-point drift after many
# +=/-= operations). Was Float; the live column needs a one-time
# manual ALTER TABLE to match (not an automatic startup self-heal —
# a column type change is a heavier, table-rewriting operation than
# the ADD COLUMN/ADD CONSTRAINT helpers in app/database.py).
# ════════════════════════════════════════════════
class Wallet(Base):
    __tablename__ = "wallets"

    id          = Column(Integer, primary_key=True, index=True)
    balance     = Column(Integer, default=0)
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
    amount      = Column(Integer, nullable=False)          # UGX — see Wallet.balance's comment on why Integer, not Float
    type        = Column(String, nullable=False)          # topup | payment
    status      = Column(String, default="pending")       # pending | completed | failed
    reference   = Column(String, nullable=True, unique=True)  # Yo Uganda ExternalReference
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
    # When tag_uid was filled in. A link with no purchase since can be
    # undone without retiring the card (POST /students/{id}/undo-card-link).
    # NULL for links made before this existed: those cannot be undone.
    linked_at      = Column(DateTime, nullable=True)
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
    amount    = Column(Integer, nullable=False)     # UGX — see Wallet.balance's comment on why Integer, not Float
    status    = Column(String, nullable=False)     # completed | failed
    reference = Column(String, nullable=True, unique=True)  # idempotency key
    timestamp = Column(DateTime, default=datetime.utcnow)

    # Relationships
    wallet = relationship("Wallet", back_populates="payments")


# ════════════════════════════════════════════════
# PAYOUTS
# End-of-day merchant settlement — one row per (merchant, calendar
# date) being paid out, enforced by the unique constraint below. This
# row is the idempotency mechanism for app/routes/reports.py's payout
# endpoints: it's inserted and committed BEFORE disburse_to_merchant()
# is ever called, so a second concurrent trigger for the same
# merchant/date either loses the INSERT race at this constraint (never
# calls Yo) or sees an already-"pending"/"sent" row and skips. Only a
# "failed" row is retried — reused in place, never a second row, since
# the unique constraint permits exactly one row per (merchant, date)
# regardless of how many attempts it takes.
# ════════════════════════════════════════════════
class Payout(Base):
    __tablename__ = "payouts"

    id           = Column(Integer, primary_key=True, index=True)
    merchant_id  = Column(Integer, ForeignKey("merchants.id"), nullable=False)
    payout_date  = Column(Date, nullable=False)
    amount       = Column(Integer, nullable=False)   # UGX
    # pending | sent | failed | indeterminate
    #   pending       → reserved; a send is in flight
    #   sent          → Yo returned SUCCEEDED
    #   failed        → money definitively did not move; a retry is allowed
    #   indeterminate → fate unknown; NEVER re-send, resolve by polling Yo
    # This is a String column, so "indeterminate" needs no live migration —
    # deliberate, given the payments.timestamp drift already on record in
    # CLAUDE.md. See _classify_payout_result() in app/routes/reports.py.
    status       = Column(String, nullable=False, default="pending")
    # The ExternalReference of the LAST attempt, written BEFORE the send so
    # a process that dies mid-call still leaves the reference the resolver
    # needs. Deterministic: SW-PAYOUT-{merchant_id}-{YYYYMMDD}-{attempt};
    # the trailing attempt number is what _next_attempt() reads back.
    yo_reference = Column(String, nullable=True)
    created_at   = Column(DateTime, default=datetime.utcnow)
    completed_at = Column(DateTime, nullable=True)

    __table_args__ = (
        UniqueConstraint("merchant_id", "payout_date", name="uq_payouts_merchant_date"),
    )

    # Relationships
    merchant = relationship("Merchant")


# ════════════════════════════════════════════════
# CARD ORDERS
# A Smart Card paid for from the parent app, for a child who already
# exists (the USSD flow creates the child and the card slot together;
# this is the card on its own). See app/routes/cards.py.
#
# Deliberately NOT a row in `transactions`: webhook.py credits the
# wallet for any pending Transaction whose reference Yo confirms, so a
# card fee recorded there would be paid into the child's wallet as if
# it were a top-up. Keeping card fees in their own table means the
# webhook finds nothing for a CARD-… reference and does nothing.
#
# status: pending | paid | failed | fulfilled | refunded
#   pending   → charge sent, parent has not approved yet
#   paid      → Yo confirmed the money; the school owes the child a card
#   failed    → rejected, timed out, or the charge never started
#   fulfilled → a card was linked to the child after payment
#   refunded  → paid, never issued, fee returned when the account closed
# ════════════════════════════════════════════════
class CardOrder(Base):
    __tablename__ = "card_orders"

    id           = Column(Integer, primary_key=True, index=True)
    student_id   = Column(Integer, ForeignKey("students.id"), nullable=False, index=True)
    ordered_by   = Column(Integer, ForeignKey("users.id"), nullable=False)
    card_color   = Column(String, nullable=False)    # Blue | Green | Yellow | Red
    amount       = Column(Integer, nullable=False)   # UGX
    status       = Column(String, nullable=False, default="pending")
    reference    = Column(String, nullable=False, unique=True)  # Yo ExternalReference, CARD-{uuid}
    momo_phone   = Column(String, nullable=False)
    network      = Column(String, nullable=False)
    created_at   = Column(DateTime, default=datetime.utcnow)
    paid_at      = Column(DateTime, nullable=True)
    fulfilled_at = Column(DateTime, nullable=True)

    # Relationships
    student = relationship("Student")


# ════════════════════════════════════════════════
# ACCOUNT CLOSURES
# A parent deleting their account. See app/closures.py for the lifecycle:
#
#   held → (72 hours) → refund sent → completed
#
# status:
#   held          → requested; wallets frozen, cards closed, signed out.
#                   Only an operator can cancel, and only now.
#   ready         → balances debited into refund_amount; not yet sent
#   pending       → refund recorded and being sent to Yo
#   failed        → Yo confirmed the refund did not move; retry allowed
#   indeterminate → fate unknown; NEVER re-send, resolve by polling Yo
#   needs_human   → automated retries used up; an operator retries
#   completed     → refund sent (or nothing owed) and the person removed
#   cancelled     → undone by an operator during the hold
#
# This row outlives the person on purpose: it is the proof the money was
# returned. refund_phone is dropped after the retention period.
# There is no column on `users` for this, so no migration must run
# before the code that reads `users` is deployed.
# ════════════════════════════════════════════════
class AccountClosure(Base):
    __tablename__ = "account_closures"

    id            = Column(Integer, primary_key=True, index=True)
    user_id       = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    status        = Column(String, nullable=False, default="held")
    requested_via = Column(String, nullable=False)   # app | web
    refund_phone  = Column(String, nullable=True)    # the registered number; dropped after retention
    refund_amount = Column(Integer, nullable=True)   # UGX, fixed when the wallets are debited
    attempts      = Column(Integer, nullable=False, default=0)
    yo_reference  = Column(String, nullable=True)    # NUV-CLOSE-{id}-{attempt}, last attempt
    # What the request froze, so a cancel restores exactly that and not
    # a wallet the parent had frozen themselves. JSON.
    frozen_wallet_ids = Column(Text, nullable=False, default="[]")
    closed_cards      = Column(Text, nullable=False, default="{}")   # {card_id: previous status}
    requested_at  = Column(DateTime, nullable=False, default=datetime.utcnow)
    process_after = Column(DateTime, nullable=False)
    last_sent_at  = Column(DateTime, nullable=True)
    refunded_at   = Column(DateTime, nullable=True)
    completed_at  = Column(DateTime, nullable=True)
    cancelled_at  = Column(DateTime, nullable=True)


# ════════════════════════════════════════════════
# PHONE VERIFICATIONS
# Proof that a parent holds their account phone, by a 6-digit SMS code.
# Needed before roster children are attached by phone number: signup
# does not verify the number, so without this anyone could register a
# guardian's number first and receive that family's children.
#
# One row per code sent. verified_at set on the row that was confirmed;
# a user with any verified row for their current phone is verified.
# The code itself is never stored, only an HMAC of it.
# ════════════════════════════════════════════════
class PhoneVerification(Base):
    __tablename__ = "phone_verifications"

    id          = Column(Integer, primary_key=True, index=True)
    user_id     = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    phone       = Column(String, nullable=False)
    code_hash   = Column(String, nullable=False)
    attempts    = Column(Integer, nullable=False, default=0)
    sent_at     = Column(DateTime, nullable=False, default=datetime.utcnow)
    expires_at  = Column(DateTime, nullable=False)
    verified_at = Column(DateTime, nullable=True)
