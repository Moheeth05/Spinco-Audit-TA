"""Regression guard over the synthetic generalization benchmark.

Every scenario in tests/synthetic_cases.py is rendered to a real PDF, pushed through the real
upload -> extraction -> matching -> validation pipeline, and scored against the generator's
exact ground truth (benchmarks/scoring.py). Two tiers:

  SAFETY invariants - asserted for EVERY family, including layouts/formats the parser is known
  not to read (heldout4): no false-confident match, no date guessed or wrong, no false duplicate,
  a duplicate/unsupported claim is never verified, and every expected exception is still raised.
  (conftest.py additionally fails any test that makes a real httpx.post call, so provider calls
  are asserted to be zero as well.)

  COVERAGE invariants - asserted only for the families the implementation was built against
  (dev, heldout, heldout2, heldout3): every claim row extracted and every supported claim
  correctly allocated. heldout4 is the one-shot unbiased family: its known gaps (label-per-line
  layout, bank-alert text) are listed in the benchmark report, not hidden by a weakened assertion.
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "benchmarks"))

import scoring  # noqa: E402
import synthetic_cases  # noqa: E402
import synthetic_reports as sr  # noqa: E402
from app.database import SessionLocal  # noqa: E402

COVERED = {"dev", "heldout", "heldout2", "heldout3"}
FAMILIES = ["dev", "heldout", "heldout2", "heldout3", "heldout4"]
PARAMS = [
    pytest.param(family, index, id=f"{family}-{scenario.__name__}")
    for family in FAMILIES
    for index, scenario in enumerate(synthetic_cases.SCENARIOS)
]


@pytest.mark.parametrize("family,index", PARAMS)
def test_synthetic_case_through_the_real_pipeline(client, family, index):
    case = synthetic_cases.build_cases(family, 1)[index]
    case.er_id = f"ER-{70000 + index}"
    pdf, page_of = sr.render_er(case)
    resp = client.post("/api/uploads/expense-reports", files=[("files", (f"{case.er_id}.pdf", pdf, "application/pdf"))])
    assert resp.status_code == 200, resp.text
    report_id = int(resp.json()["results"][0]["document_id"].split(":")[1])
    db = SessionLocal()
    try:
        result = scoring.score_case(case, page_of, db, report_id)
    finally:
        db.close()

    # ---- safety: always
    for claim in result["claims"]:
        if not claim.get("found"):
            continue
        assert not claim["false_confident"], (case.name, claim)
        assert not claim["false_duplicate"], (case.name, claim)
        if claim["duplicate_expected"]:
            assert claim["duplicate_not_verified"], (case.name, claim)
        if claim["exception_expected"]:
            assert claim["exception_found"], (case.name, claim)
    for receipt in result["receipts"]:
        if receipt["found"]:
            assert receipt["date_state"] not in ("wrong", "guessed_ambiguous"), (case.name, receipt)

    # ---- coverage: only where the implementation is expected to read everything
    if family in COVERED:
        row = result["row"]
        assert row["exact_rows"] == row["truth_rows"] == row["predicted_rows"], (case.name, row)
        for claim in result["claims"]:
            if claim["expect"] == "SUPPORTED":
                assert claim["allocation_correct"], (case.name, claim)
                if not claim["exception_expected"]:
                    assert claim["fully_verified"], (case.name, claim)
