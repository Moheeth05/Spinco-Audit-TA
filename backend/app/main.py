from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app import models  # noqa: F401  (register all models on Base before create_all)
from app.api import analytics, dashboard, excel_files, expense_reports, health, policy, trip_reports, users
from app.database import Base, engine
from app.seed import seed_all

app = FastAPI(title="SPINCO TA / Travel Expense Audit & Automation System")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5200", "http://127.0.0.1:5200"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.on_event("startup")
def on_startup():
    Base.metadata.create_all(bind=engine)
    seed_all()


app.include_router(health.router)
app.include_router(policy.router)
app.include_router(users.router)
app.include_router(dashboard.router)
app.include_router(expense_reports.router)
app.include_router(trip_reports.router)
app.include_router(excel_files.router)
app.include_router(analytics.router)
