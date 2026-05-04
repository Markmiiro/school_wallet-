from fastapi import FastAPI
from app.database import engine, Base
import app.models

app = FastAPI()

Base.metadata.create_all(bind=engine)

@app.get("/")
def home():
    return {"message": "Backend running 🚀"}