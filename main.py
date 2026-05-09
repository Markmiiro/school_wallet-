from fastapi import FastAPI
from app.database import test_connection, create_tables

from app.routes import schools
from app.routes import users

from app.routes import students
from app.routes import wallets

app = FastAPI()

app.include_router(schools.router)
app.include_router(users.router)
app.include_router(students.router)
app.include_router(wallets.router)



@app.on_event("startup")
def startup():
    test_connection()
    create_tables()


@app.get("/")
def home():
    return {"message": "Backend running 🚀"}