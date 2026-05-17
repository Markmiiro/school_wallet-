# ================================================
# app/routes/reports.py
# ------------------------------------------------
# Phase 4 — Multi-vendor reporting and settlement
#
# ENDPOINTS:
# GET  /reports/merchant/{id}/daily      → merchant daily report
# GET  /reports/merchant/{id}/dashboard  → merchant summary
# GET  /reports/school/{id}/settlement   → admin settlement report
# POST /reports/school/{id}/payout       → trigger manual payout
# POST /reports/settlements/auto         → automated daily payout
# ================================================

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session
from sqlalchemy import func
from datetime import datetime, date, timedelta
from typing import Optional

from app.database import get_db
from app.models import Transaction, Wallet, Student, Merchant, School, User
from app.momo import disburse_to_merchant

router = APIRouter()


# ================================================
# ENDPOINT 1 — Merchant daily sales report
# ================================================
# GET /reports/merchant/{merchant_id}/daily
#
# Shows a merchant exactly what they sold today
# or on any specific date.
# ================================================
@router.get("/merchant/{merchant_id}/daily")
def merchant_daily_report(
    merchant_id: int,
    report_date: Optional[str] = Query(
        default=None,
        description="Date in YYYY-MM-DD format. Defaults to today."
    ),
    db: Session = Depends(get_db)
):
    """
    Full daily sales report for a merchant.
    Shows every transaction, totals, and comparison to yesterday.
    """
    # ── Get merchant ────────────────────────────
    merchant = db.query(Merchant).filter(
        Merchant.id == merchant_id
    ).first()
    if not merchant:
        raise HTTPException(status_code=404, detail="Merchant not found")

    # ── Parse date ──────────────────────────────
    if report_date:
        try:
            target_date = datetime.strptime(report_date, "%Y-%m-%d").date()
        except ValueError:
            raise HTTPException(
                status_code=400,
                detail="Invalid date format. Use YYYY-MM-DD e.g. 2026-05-17"
            )
    else:
        target_date = date.today()

    yesterday = target_date - timedelta(days=1)

    # ── Get today's transactions ─────────────────
    today_txns = (
        db.query(Transaction)
        .filter(
            Transaction.merchant_id == merchant_id,
            Transaction.type == "payment",
            Transaction.status == "completed",
        )
        .all()
    )

    # Filter by date manually (SQLite compatible)
    today_txns = [
        t for t in today_txns
        if t.timestamp and t.timestamp.date() == target_date
    ]

    # ── Get yesterday's transactions ─────────────
    yesterday_txns = (
        db.query(Transaction)
        .filter(
            Transaction.merchant_id == merchant_id,
            Transaction.type == "payment",
            Transaction.status == "completed",
        )
        .all()
    )
    yesterday_txns = [
        t for t in yesterday_txns
        if t.timestamp and t.timestamp.date() == yesterday
    ]

    # ── Calculate totals ─────────────────────────
    today_total     = sum(t.amount for t in today_txns)
    yesterday_total = sum(t.amount for t in yesterday_txns)

    # ── Calculate change ─────────────────────────
    if yesterday_total > 0:
        change_pct = ((today_total - yesterday_total) / yesterday_total) * 100
        change_str = f"+{change_pct:.1f}%" if change_pct >= 0 else f"{change_pct:.1f}%"
    else:
        change_str = "N/A (no sales yesterday)"

    # ── Build transaction breakdown ──────────────
    breakdown = []
    for txn in sorted(today_txns, key=lambda x: x.timestamp, reverse=True):
        # Get student name
        wallet = db.query(Wallet).filter(Wallet.id == txn.wallet_id).first()
        student_name = "Unknown"
        if wallet:
            student = db.query(Student).filter(
                Student.id == wallet.student_id
            ).first()
            if student:
                student_name = student.name

        breakdown.append({
            "transaction_id": txn.id,
            "time":           txn.timestamp.strftime("%I:%M %p") if txn.timestamp else "N/A",
            "student":        student_name,
            "amount":         txn.amount,
            "description":    txn.description or "Payment",
        })

    # ── Busiest hour ─────────────────────────────
    if today_txns:
        hours = [t.timestamp.hour for t in today_txns if t.timestamp]
        if hours:
            busiest_hour = max(set(hours), key=hours.count)
            busiest_str  = f"{busiest_hour:02d}:00 - {busiest_hour:02d}:59"
        else:
            busiest_str = "N/A"
    else:
        busiest_str = "N/A"

    return {
        "merchant":          merchant.name,
        "merchant_id":       merchant_id,
        "school_id":         merchant.school_id,
        "report_date":       str(target_date),
        "summary": {
            "total_sales_ugx":        today_total,
            "number_of_transactions": len(today_txns),
            "average_transaction_ugx": round(today_total / len(today_txns)) if today_txns else 0,
            "busiest_hour":           busiest_str,
        },
        "comparison": {
            "today_ugx":     today_total,
            "yesterday_ugx": yesterday_total,
            "change":        change_str,
        },
        "transactions": breakdown,
        "payout_status": {
            "amount_to_receive": today_total,
            "payout_phone":      merchant.momo_phone,
            "note": "Payout sent daily at 6:00 PM automatically"
        }
    }


# ================================================
# ENDPOINT 2 — Merchant dashboard summary
# ================================================
# GET /reports/merchant/{merchant_id}/dashboard
#
# Overview of today, this week, and this month.
# ================================================
@router.get("/merchant/{merchant_id}/dashboard")
def merchant_dashboard(
    merchant_id: int,
    db: Session = Depends(get_db)
):
    """
    Full merchant dashboard summary.
    Shows today, this week, and this month at a glance.
    """
    merchant = db.query(Merchant).filter(
        Merchant.id == merchant_id
    ).first()
    if not merchant:
        raise HTTPException(status_code=404, detail="Merchant not found")

    today      = date.today()
    week_start = today - timedelta(days=today.weekday())
    month_start = today.replace(day=1)

    # Get all completed payments for this merchant
    all_txns = (
        db.query(Transaction)
        .filter(
            Transaction.merchant_id == merchant_id,
            Transaction.type == "payment",
            Transaction.status == "completed",
        )
        .all()
    )

    # Filter by period
    today_txns  = [t for t in all_txns if t.timestamp and t.timestamp.date() == today]
    week_txns   = [t for t in all_txns if t.timestamp and t.timestamp.date() >= week_start]
    month_txns  = [t for t in all_txns if t.timestamp and t.timestamp.date() >= month_start]

    # Totals
    today_total = sum(t.amount for t in today_txns)
    week_total  = sum(t.amount for t in week_txns)
    month_total = sum(t.amount for t in month_txns)

    # Daily breakdown for the week
    weekly_breakdown = []
    for i in range(7):
        day = week_start + timedelta(days=i)
        day_txns  = [t for t in all_txns if t.timestamp and t.timestamp.date() == day]
        day_total = sum(t.amount for t in day_txns)
        weekly_breakdown.append({
            "day":         day.strftime("%A %d %b"),
            "total_ugx":   day_total,
            "num_sales":   len(day_txns),
        })

    return {
        "merchant":    merchant.name,
        "merchant_id": merchant_id,
        "as_of":       str(today),
        "today": {
            "total_ugx":   today_total,
            "num_sales":   len(today_txns),
        },
        "this_week": {
            "total_ugx":   week_total,
            "num_sales":   len(week_txns),
            "daily_breakdown": weekly_breakdown,
        },
        "this_month": {
            "total_ugx":   month_total,
            "num_sales":   len(month_txns),
        },
        "payout_phone": merchant.momo_phone,
    }


# ================================================
# ENDPOINT 3 — School admin settlement report
# ================================================
# GET /reports/school/{school_id}/settlement
#
# Shows ALL vendors, their sales, and what to
# pay each one. The bursar's end-of-day view.
# ================================================
@router.get("/school/{school_id}/settlement")
def school_settlement_report(
    school_id: int,
    report_date: Optional[str] = Query(
        default=None,
        description="Date in YYYY-MM-DD format. Defaults to today."
    ),
    db: Session = Depends(get_db)
):
    """
    Full settlement report for school admin.
    Shows every vendor's sales and payout amounts.
    """
    # ── Get school ──────────────────────────────
    school = db.query(School).filter(School.id == school_id).first()
    if not school:
        raise HTTPException(status_code=404, detail="School not found")

    # ── Parse date ──────────────────────────────
    if report_date:
        try:
            target_date = datetime.strptime(report_date, "%Y-%m-%d").date()
        except ValueError:
            raise HTTPException(
                status_code=400,
                detail="Invalid date format. Use YYYY-MM-DD"
            )
    else:
        target_date = date.today()

    # ── Get all merchants for this school ────────
    merchants = db.query(Merchant).filter(
        Merchant.school_id == school_id,
        Merchant.is_active == True,
    ).all()

    if not merchants:
        return {
            "school":       school.name,
            "report_date":  str(target_date),
            "message":      "No active merchants found for this school"
        }

    # ── Build report for each merchant ──────────
    vendor_reports = []
    grand_total    = 0

    for merchant in merchants:
        # Get this merchant's transactions for the day
        txns = (
            db.query(Transaction)
            .filter(
                Transaction.merchant_id == merchant.id,
                Transaction.type == "payment",
                Transaction.status == "completed",
            )
            .all()
        )

        day_txns = [
            t for t in txns
            if t.timestamp and t.timestamp.date() == target_date
        ]

        merchant_total = sum(t.amount for t in day_txns)
        grand_total   += merchant_total

        # Full transaction breakdown
        transaction_details = []
        for t in sorted(day_txns, key=lambda x: x.timestamp, reverse=True):
            wallet = db.query(Wallet).filter(Wallet.id == t.wallet_id).first()
            student_name = "Unknown"
            if wallet:
                student = db.query(Student).filter(
                    Student.id == wallet.student_id
                ).first()
                if student:
                    student_name = student.name

            transaction_details.append({
                "time":        t.timestamp.strftime("%I:%M %p") if t.timestamp else "N/A",
                "student":     student_name,
                "amount_ugx":  t.amount,
                "description": t.description or "Payment",
            })

        vendor_reports.append({
            "merchant_id":        merchant.id,
            "merchant_name":      merchant.name,
            "payout_phone":       merchant.momo_phone,
            "total_sales_ugx":    merchant_total,
            "number_of_sales":    len(day_txns),
            "payout_status":      "pending",
            "transactions":       transaction_details,
        })

    return {
        "school":        school.name,
        "school_id":     school_id,
        "report_date":   str(target_date),
        "grand_total_ugx": grand_total,
        "number_of_vendors": len(merchants),
        "vendors":       vendor_reports,
        "generated_at":  datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC"),
    }


# ================================================
# ENDPOINT 4 — Manual payout trigger
# ================================================
# POST /reports/school/{school_id}/payout
#
# Admin manually triggers payout to all vendors.
# Uses DGateway disburse to send money to each
# merchant's MoMo number.
# ================================================
@router.post("/school/{school_id}/payout")
async def trigger_manual_payout(
    school_id: int,
    report_date: Optional[str] = Query(default=None),
    db: Session = Depends(get_db)
):
    """
    Trigger end-of-day payout to all merchants.
    Sends each vendor's daily sales to their MoMo number.
    Can also be triggered automatically at 6PM.
    """
    school = db.query(School).filter(School.id == school_id).first()
    if not school:
        raise HTTPException(status_code=404, detail="School not found")

    # Parse date
    if report_date:
        try:
            target_date = datetime.strptime(report_date, "%Y-%m-%d").date()
        except ValueError:
            raise HTTPException(status_code=400, detail="Invalid date format")
    else:
        target_date = date.today()

    merchants = db.query(Merchant).filter(
        Merchant.school_id == school_id,
        Merchant.is_active == True,
    ).all()

    payouts_sent   = []
    payouts_failed = []

    for merchant in merchants:
        # Skip merchants with no payout phone
        if not merchant.momo_phone:
            payouts_failed.append({
                "merchant": merchant.name,
                "reason":   "No MoMo phone number set"
            })
            continue

        # Get daily total
        txns = (
            db.query(Transaction)
            .filter(
                Transaction.merchant_id == merchant.id,
                Transaction.type == "payment",
                Transaction.status == "completed",
            )
            .all()
        )

        day_txns = [
            t for t in txns
            if t.timestamp and t.timestamp.date() == target_date
        ]

        merchant_total = sum(t.amount for t in day_txns)

        # Skip if no sales
        if merchant_total == 0:
            payouts_failed.append({
                "merchant": merchant.name,
                "reason":   "No sales today — nothing to pay out"
            })
            continue

        # Send payout via DGateway
        try:
            result = await disburse_to_merchant(
                phone=merchant.momo_phone,
                amount=merchant_total,
                merchant_name=merchant.name,
            )

            payouts_sent.append({
                "merchant":    merchant.name,
                "phone":       merchant.momo_phone,
                "amount_ugx":  merchant_total,
                "status":      "sent",
                "reference":   result.get("data", {}).get("reference", "N/A"),
            })

            print(f"✅ Payout sent: {merchant.name} UGX {merchant_total:,}")

        except Exception as e:
            payouts_failed.append({
                "merchant": merchant.name,
                "reason":   str(e),
            })
            print(f"❌ Payout failed: {merchant.name} — {e}")

    total_paid = sum(p["amount_ugx"] for p in payouts_sent)

    return {
        "school":        school.name,
        "payout_date":   str(target_date),
        "total_paid_ugx": total_paid,
        "payouts_sent":  len(payouts_sent),
        "payouts_failed": len(payouts_failed),
        "details": {
            "sent":   payouts_sent,
            "failed": payouts_failed,
        }
    }


# ================================================
# ENDPOINT 5 — Automated daily payout
# ================================================
# POST /reports/settlements/auto
#
# Called automatically at 6PM every day.
# Processes payouts for ALL schools at once.
# ================================================
@router.post("/settlements/auto")
async def automated_daily_payout(
    secret: str = Query(..., description="Secret key to prevent unauthorized calls"),
    db: Session = Depends(get_db)
):
    """
    Automated end-of-day payout for ALL schools.
    Called by a cron job at 6PM every day.
    Requires secret key for security.
    """
    import os
    expected_secret = os.getenv("SETTLEMENT_SECRET", "school_wallet_settle_2026")

    if secret != expected_secret:
        raise HTTPException(
            status_code=403,
            detail="Invalid secret key"
        )

    today = date.today()

    # Get all active schools
    schools = db.query(School).all()

    results = []

    for school in schools:
        merchants = db.query(Merchant).filter(
            Merchant.school_id == school.id,
            Merchant.is_active == True,
        ).all()

        school_total = 0
        school_payouts = []

        for merchant in merchants:
            if not merchant.momo_phone:
                continue

            txns = (
                db.query(Transaction)
                .filter(
                    Transaction.merchant_id == merchant.id,
                    Transaction.type == "payment",
                    Transaction.status == "completed",
                )
                .all()
            )

            day_txns = [
                t for t in txns
                if t.timestamp and t.timestamp.date() == today
            ]

            merchant_total = sum(t.amount for t in day_txns)

            if merchant_total == 0:
                continue

            try:
                result = await disburse_to_merchant(
                    phone=merchant.momo_phone,
                    amount=merchant_total,
                    merchant_name=merchant.name,
                )
                school_total += merchant_total
                school_payouts.append({
                    "merchant":   merchant.name,
                    "amount_ugx": merchant_total,
                    "status":     "sent",
                })
            except Exception as e:
                school_payouts.append({
                    "merchant":   merchant.name,
                    "amount_ugx": merchant_total,
                    "status":     f"failed: {e}",
                })

        results.append({
            "school":      school.name,
            "total_ugx":   school_total,
            "payouts":     school_payouts,
        })

    grand_total = sum(r["total_ugx"] for r in results)

    print(f"\n🏦 Auto settlement complete: UGX {grand_total:,} across {len(schools)} schools")

    return {
        "date":        str(today),
        "schools":     len(schools),
        "grand_total_ugx": grand_total,
        "results":     results,
        "completed_at": datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC"),
    }