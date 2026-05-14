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



# ── Create the app ──────────────────────────────
app = FastAPI(
    title="🏫 School Wallet API",
    description="Cashless payment system for schools in Uganda 🇺🇬",
    version="1.0.0",
)

# ── CORS ────────────────────────────────────────
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
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
app.include_router(schools.router,  prefix="/schools",  tags=["Schools"])
app.include_router(users.router,    prefix="/users",    tags=["Users"])
app.include_router(students.router, prefix="/students", tags=["Students"])
app.include_router(wallets.router,  prefix="/wallets",  tags=["Wallets"])
app.include_router(topup.router,    prefix="/topup",    tags=["Top-Up"])
app.include_router(webhook.router, prefix="/webhook", tags=["Webhook"])
app.include_router(merchants.router, prefix="/merchants", tags=["Merchants"])
app.include_router(payments.router, prefix="/payments", tags=["Payments"])
app.include_router(ussd.router, prefix="/ussd", tags=["USSD"])

