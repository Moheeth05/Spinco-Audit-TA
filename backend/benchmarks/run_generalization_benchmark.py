"""Generalization benchmark: synthetic Expense Reports with exact ground truth
are pushed through the REAL upload/extraction/matching/validation pipeline (in
a throwaway DB and storage) and the stored results are scored against the
labels.

    python benchmarks/run_generalization_benchmark.py --family dev heldout --seed 1 --label baseline

Providers are disabled and httpx.post is replaced by a counter that fails, so
the provider-call count is measured, not assumed. The real %APPDATA% data and
all original PDFs are never touched.

What it can and cannot measure: ground truth is what the generator wrote, so
every metric is exact - but only for the situations/templates the generator
expresses. It does not measure real handwriting, photographs or scanner noise.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
import time
from collections import Counter, defaultdict
from pathlib import Path

BACKEND = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BACKEND))
sys.path.insert(0, str(BACKEND / "tests"))

parser = argparse.ArgumentParser()
parser.add_argument("--family", nargs="+", default=["dev", "heldout"])
parser.add_argument("--seed", type=int, nargs="+", default=[1])
parser.add_argument("--label", default="run")
parser.add_argument("--out", default=None)
parser.add_argument("--only", default=None, help="substring filter on case name")
args = parser.parse_args()

_tmp = Path(tempfile.mkdtemp(prefix="spinco_bench_"))
os.environ.update({
    "SPINCO_DB_PATH": str(_tmp / "bench.db"), "SPINCO_STORAGE_ROOT": str(_tmp / "storage"),
    "SPINCO_ARGON2_TIME_COST": "1", "SPINCO_ARGON2_MEMORY_COST": "8", "SPINCO_ARGON2_PARALLELISM": "1",
})
_tess = BACKEND.parent / "resources" / "tesseract"
if _tess.exists():
    os.environ["SPINCO_TESSERACT_DIR"] = str(_tess)
for _k in list(os.environ):
    if _k.startswith(("SPINCO_NVIDIA_", "SPINCO_DEEPSEEK_")):
        del os.environ[_k]

import logging  # noqa: E402

import httpx  # noqa: E402

PROVIDER_CALLS = {"count": 0}


def _blocked_post(*a, **k):
    PROVIDER_CALLS["count"] += 1
    raise RuntimeError("benchmark: network call blocked")


httpx.post = _blocked_post
logging.disable(logging.CRITICAL)

from fastapi.testclient import TestClient  # noqa: E402

import synthetic_cases  # noqa: E402
import synthetic_reports as sr  # noqa: E402
from app.database import Base, SessionLocal, engine  # noqa: E402
from app.main import app  # noqa: E402
from app.seed import seed_all  # noqa: E402

from scoring import aggregate, summarize  # noqa: E402
from scoring import score_case  # noqa: E402


def main() -> None:
    Base.metadata.create_all(bind=engine)
    seed_all()
    all_results: dict[str, list[dict]] = {}
    timings: dict[str, float] = defaultdict(float)
    with TestClient(app) as client:
        login = client.post("/api/auth/login", json={"email": "intern@spincotech.com", "password": "ChangeMe123!"})
        headers = {"X-CSRF-Token": login.json()["csrf_token"]}
        for family in args.family:
            cases = [c for seed in args.seed for c in synthetic_cases.build_cases(family, seed)]
            for index, case in enumerate(cases):
                if args.only and args.only not in case.name:
                    continue
                case.er_id = f"ER-{60000 + index + (1000 if family != 'dev' else 0)}"
                pdf, page_of = sr.render_er(case)
                t0 = time.time()
                resp = client.post("/api/uploads/expense-reports", files=[("files", (f"{case.er_id}.pdf", pdf, "application/pdf"))], headers=headers)
                timings[family] += time.time() - t0
                item = resp.json()["results"][0]
                report_id = int(item["document_id"].split(":")[1])
                db = SessionLocal()
                try:
                    all_results.setdefault(family, []).append(score_case(case, page_of, db, report_id))
                finally:
                    db.close()
                print(f"  [{family}] {case.name} done ({time.time() - t0:.1f}s)", flush=True)
    report = {"label": args.label, "seeds": args.seed, "provider_calls": PROVIDER_CALLS["count"], "families": {}}
    text = []
    for family, results in all_results.items():
        agg = aggregate(results)
        report["families"][family] = {"aggregate": agg, "seconds": round(timings[family], 1), "cases": results}
        text.append(summarize(f"{args.label} / {family} ({len(results)} cases, {agg.get('claims', 0)} claims, {agg.get('receipts', 0)} receipts, {timings[family]:.0f}s)", agg))
    text.append(f"provider calls attempted: {PROVIDER_CALLS['count']}")
    print("\n" + "\n\n".join(text))
    out = Path(args.out) if args.out else BACKEND / "benchmarks" / "results" / f"{args.label}.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, indent=1, default=str), encoding="utf-8")
    print(f"\nwritten: {out}")


if __name__ == "__main__":
    main()
