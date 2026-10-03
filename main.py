import os

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from app.database import test_connection, create_tables

from app.routes import schools
from app.routes import users
from app.routes import students
from app.routes import wallets
from app.routes import topup
from app.routes import webhook
from app.routes import merchants
from app.routes import payments
from app.routes import ussd
from app.routes import reports
from app.routes import analytics
from app.routes import auth
from app.routes import tuckshop
from app.routes import issue
from app.routes import diagnostics
from app.routes import cards
from app.routes import account




# ── Create the app ──────────────────────────────
app = FastAPI(
    title="🏫 School Wallet API",
    description="Cashless payment system for schools in Uganda 🇺🇬",
    version="1.0.0",
)

# ── CORS ────────────────────────────────────────
# Only browser front-ends we actually ship may call this API from
# another origin. The tuck shop and card pages are served by this app
# itself (same origin) and native mobile builds are not subject to CORS,
# so neither needs an entry here.
#
# CORS_ORIGINS: comma-separated list, set in Railway → Variables to add
# or change origins without a code change. Local development on any
# localhost port is always allowed. Auth is a bearer header, not a
# cookie, so credentials are not enabled.
_DEFAULT_CORS_ORIGINS = "https://nuvora-ug.netlify.app"
CORS_ORIGINS = [
    o.strip().rstrip("/")
    for o in os.getenv("CORS_ORIGINS", _DEFAULT_CORS_ORIGINS).split(",")
    if o.strip()
]

app.add_middleware(
    CORSMiddleware,
    allow_origins=CORS_ORIGINS,
    allow_origin_regex=r"http://(localhost|127\.0\.0\.1)(:\d+)?",
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ── Startup ─────────────────────────────────────
@app.on_event("startup")
def startup():
    print("\n🚀 School Wallet API starting...")
    test_connection()
    create_tables()
    print("✅ Ready — visit http://localhost:8000/docs\n")

# ── Health check ────────────────────────────────
@app.get("/", tags=["Health"])
def home():
    return {
        "status": "running",
        "message": "School Wallet API is live 🏫",
        "docs": "http://localhost:8000/docs"
    }

# ── Routes ──────────────────────────────────────
# Every router needs BOTH a prefix AND tags
# prefix → sets the URL  e.g. /schools/
# tags   → sets the label in Swagger UI
app.include_router(schools.router,   prefix="/schools",   tags=["Schools"])
app.include_router(users.router,     prefix="/users",     tags=["Users"])
app.include_router(students.router,  prefix="/students",  tags=["Students"])
app.include_router(wallets.router,   prefix="/wallets",   tags=["Wallets"])
app.include_router(topup.router,     prefix="/topup",     tags=["Top-Up"])
app.include_router(webhook.router,   prefix="/webhook",   tags=["Webhook"])
app.include_router(merchants.router, prefix="/merchants", tags=["Merchants"])
app.include_router(payments.router,  prefix="/payments",  tags=["Payments"])
app.include_router(ussd.router,      prefix="/ussd",      tags=["USSD"])
app.include_router(reports.router,   prefix="/reports",   tags=["Reports & Settlement"])
app.include_router(analytics.router, prefix="/analytics", tags=["Analytics"])
app.include_router(auth.router, prefix="/auth", tags=["Authentication"])
app.include_router(tuckshop.router, prefix="/tuckshop", tags=["Tuck Shop"])
app.include_router(issue.router, prefix="/issue", tags=["Card Issuance"])
app.include_router(cards.router, prefix="/cards", tags=["Card Orders"])
app.include_router(account.router, prefix="/account", tags=["Account"])
app.include_router(diagnostics.router, prefix="/diagnostics", tags=["Diagnostics"])