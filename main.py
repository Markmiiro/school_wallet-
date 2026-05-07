from fastapi import FastAPI
from app.database import engine, Base ,test_connection,create_tables
import app.models 

app = FastAPI()

Base.metadata.create_all(bind=engine)

@app.on_event("startup")
def startup():
    test_connection()
    create_tables()

@app.get("/")
def home():
    return {"message": "Backend running 🚀"}