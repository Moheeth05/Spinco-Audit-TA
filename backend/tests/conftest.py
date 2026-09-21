import os
import shutil
import tempfile
from pathlib import Path

import pytest

_TEST_DIR = Path(tempfile.mkdtemp(prefix="spinco_test_"))
os.environ["SPINCO_DB_PATH"] = str(_TEST_DIR / "test.db")
os.environ["SPINCO_STORAGE_ROOT"] = str(_TEST_DIR / "storage")

from fastapi.testclient import TestClient  # noqa: E402

from app.database import Base, engine  # noqa: E402
from app.main import app  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parent.parent.parent


@pytest.fixture(scope="session", autouse=True)
def _cleanup_test_dir():
    yield
    shutil.rmtree(_TEST_DIR, ignore_errors=True)


@pytest.fixture()
def client():
    """Fresh schema per test - Phase 2 has no migration framework yet, so tests
    that need isolation just recreate the tables."""
    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)
    with TestClient(app) as c:
        yield c


@pytest.fixture()
def sample_pdf_path():
    return REPO_ROOT / "ER-39832.pdf"


@pytest.fixture()
def sample_trip_pdf_path():
    return REPO_ROOT / "TRIP-24935_Trip_To_Indore_Pithampur.pdf"
