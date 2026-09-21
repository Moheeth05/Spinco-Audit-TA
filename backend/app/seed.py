"""Idempotent seed data: the 29 policy-master categories (transcribed verbatim from
the company policy spec) and the initial ADMIN user. No expense/trip/business
records are ever seeded here - those only exist once real documents are processed."""

from datetime import date

from sqlalchemy import select

from app.database import SessionLocal
from app.models.policy import PolicyMaster
from app.models.user import User

EFFECTIVE_DATE = date(2026, 1, 1)

# (category, reimbursement_basis, limit_amount, document_required, claim_type)
POLICY_CATEGORIES: list[tuple[str, str, float | None, str, str]] = [
    ("AIRFARE", "ACTUAL", None, "DA", "NR"),
    ("BATTA", "ACTUAL", None, "DNA", "REGULAR"),
    ("CAB CHARGES", "ACTUAL", None, "DA", "REGULAR"),
    ("CALIBRATION CHARGE", "LIMIT", 500, "YES", "NR"),
    ("COMPANY CAR MAINTENANCE", "ACTUAL", None, "DNA", "NR"),
    ("COMPANY CAR PETROL", "LIMIT", 8500, "YES", "NR"),
    ("COURIER CHARGES", "LIMIT", 500, "YES", "NR"),
    ("INTERNET CHARGES", "LIMIT", 300, "YES", "REGULAR"),
    ("LOCAL CONVEYANCE", "ACTUAL", None, "DNA", "REGULAR"),
    ("LODGING", "ACTUAL", None, "DA", "REGULAR"),
    ("MARKETING - SEMINAR EXPS", "LIMIT", 500, "YES", "NR"),
    ("MARKETING EXPENSES - FOOD", "LIMIT", 500, "YES", "NR"),
    ("MARKETING EXPENSES - OTHERS", "LIMIT", 500, "YES", "NR"),
    ("MEMBERSHIP & SUBSCRIPTION FEE", "LIMIT", 500, "YES", "NR"),
    ("OFFICE EXPENSES - DRIVER SALARY", "ACTUAL", None, "DNA", "NR"),
    ("OWN CAR PETROL", "LIMIT", 8500, "YES", "REGULAR"),
    ("PRINTING & STATIONERY", "LIMIT", 500, "YES", "NR"),
    ("RATES & TAXES", "LIMIT", 500, "YES", "NR"),
    ("REPAIRS - COMPUTERS", "LIMIT", 500, "YES", "NR"),
    ("SERVICE PURCHASES/SERVICE CHARGE", "LIMIT", 500, "YES", "NR"),
    ("STAFF WELFARE", "LIMIT", 500, "YES", "NR"),
    ("TELEPHONE ACCESSORIES", "LIMIT", 500, "YES", "NR"),
    ("TENDER FEE", "ACTUAL", None, "DA", "NR"),
    ("TOLL TAX CHARGES", "ACTUAL", None, "DA", "NR"),
    ("TRAIN / BUS FARE", "ACTUAL", None, "DA", "REGULAR"),
    ("TRANSPORT CHARGES", "LIMIT", 500, "YES", "NR"),
    ("TRAVEL-OTHERS", "ACTUAL", None, "DA", "NR"),
    ("TWO WHEELER MAINTENANCE", "LIMIT", 300, "NO", "REGULAR"),
    ("TWO WHEELER PETROL", "LIMIT", 3500, "YES", "REGULAR"),
]

ADMIN_EMAIL = "intern@spincotech.com"


def seed_policy_master(db) -> None:
    existing = {row.category for row in db.execute(select(PolicyMaster)).scalars().all()}
    for category, basis, limit_amount, doc_required, claim_type in POLICY_CATEGORIES:
        if category in existing:
            continue
        db.add(
            PolicyMaster(
                category=category,
                reimbursement_basis=basis,
                limit_amount=limit_amount,
                document_required=doc_required,
                claim_type=claim_type,
                status="ACTIVE",
                effective_date=EFFECTIVE_DATE,
                policy_version=1,
            )
        )
    db.commit()


def seed_admin_user(db) -> None:
    existing = db.execute(select(User).where(User.email == ADMIN_EMAIL)).scalars().first()
    if existing:
        return
    db.add(User(email=ADMIN_EMAIL, name="Intern", role="ADMIN"))
    db.commit()


def seed_all() -> None:
    db = SessionLocal()
    try:
        seed_policy_master(db)
        seed_admin_user(db)
    finally:
        db.close()
