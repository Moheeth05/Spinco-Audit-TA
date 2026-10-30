"""Writes the synthetic Expense Report PDFs used by the isolated packaging validation.

Usage: python benchmarks/make_packaging_fixtures.py <output_dir>

Each fixture exercises one behaviour of the packaged app; fixtures.json records what a correct
outcome is, so the packaged-app driver can assert against it. The PDFs contain only invented
names, vendors and amounts - no company data.
"""

import json
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tests"))

import synthetic_cases as sc  # noqa: E402
import synthetic_reports as sr  # noqa: E402

YEAR = sc.YEAR


def _unreadable_receipt(rid: str, day: date, merchant: str = "Rapido") -> sr.Receipt:
    """A cab booking page whose price is not printed at all: date, vendor and category are readable,
    the amount is unreadable by construction."""
    raw = f"{sr.MONTH_ABBR[day.month - 1]} {day.day}st {day.year}" if day.day in (1, 21, 31) else f"{sr.MONTH_ABBR[day.month - 1]} {day.day}th {day.year}"
    return sr.Receipt(
        rid, "unreadable_amount",
        [merchant, "Booking History", f"Ride ID RD{rid.upper()}7731", f"Time of Ride {raw}, 7:37 PM", "Mode of Vehicle Auto",
         "Palandi Amman Koil St, Triplicane, Chennai 600005"],
        None, day, "19:37", merchant, None, None, "ride", raw, True, category_evidence="Cab/Taxi",
    )


def _case(name, layout, claims, receipts, er_id):
    return sr.Case(name=name, family="packaging", layout=layout, claims=claims, receipts=receipts, er_id=er_id)


def unreadable_amount():
    day = date(YEAR, 7, 21)
    blind = _unreadable_receipt("r1", day)
    readable = sr.tpl_ola_like(__import__("random").Random(7), "r2", 120.0, date(YEAR, 7, 22), (10, 15))
    claims = [
        sr.Claim(1, day, "CAB CHARGES", "Rapido ride", "Rapido", 319.0, "UNSUPPORTED", set(), expect_exception="BILL MATCH REVIEW REQUIRED"),
        sr.Claim(2, date(YEAR, 7, 22), "CAB CHARGES", "QuickRide ride", "QuickRide", 120.0, "SUPPORTED", {"r2"}),
    ]
    return _case("unreadable_amount", "L1", claims, [blind, readable], "ER-81001")


def shared_bill():
    day = date(YEAR, 7, 21)
    rng = __import__("random").Random(11)
    owner = sr.tpl_taxi_slip_labeled(rng, "r1", 1150.0, day, (18, 30), merchant="Cab Service", fmt="DD/MM/YYYY", cue="Date (DD/MM/YYYY)")
    blind_a = _unreadable_receipt("r2", day)
    blind_b = _unreadable_receipt("r3", day)
    claims = [sr.Claim(n, day, "CAB CHARGES", "Cab Service ride", "Cab Service", 1150.0, "AMBIGUOUS", {"r1"}) for n in (1, 2, 3)]
    return _case("shared_bill", "L1", claims, [owner, blind_a, blind_b], "ER-81002")


def build() -> dict[str, sr.Case]:
    def dev(scenario, index, er_id):
        case = scenario(sc._Ctx("dev", 100 + index))
        case.er_id = er_id
        return case

    return {
        "pf_ambiguous_date": dev(sc.ambiguous_date_no_evidence, 7, "ER-81003"),
        "pf_unreadable_amount": unreadable_amount(),
        "pf_shared_bill": shared_bill(),
        "pf_repeated_amounts": dev(sc.same_amount_diff_dates, 0, "ER-81004"),
        "pf_duplicate_screenshot": dev(sc.duplicate_screenshot_reused, 3, "ER-81005"),
        "pf_flight_vs_lodging": dev(sc.flight_claimed_as_lodging, 12, "ER-81006"),
        "pf_upi_status": dev(sc.pending_failed, 5, "ER-81007"),
        "pf_ocr_pages": dev(sc.ocr_only_pages, 16, "ER-81008"),
        "pf_same_day_times": dev(sc.same_day_distinct_times, 2, "ER-81009"),
    }


def main(out_dir: Path) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    manifest = {}
    for name, case in build().items():
        pdf, page_of = sr.render_er(case)
        path = out_dir / f"{name}.pdf"
        path.write_bytes(pdf)
        manifest[name] = {
            "file": path.name,
            "er_id": case.er_id,
            "claims": [{"s_no": c.s_no, "date": c.day.isoformat(), "category": c.category, "amount": c.amount, "expect": c.expect,
                        "expect_exception": c.expect_exception} for c in case.claims],
            "receipt_pages": page_of,
        }
        print(f"wrote {path.name}: {len(case.claims)} claims, {len(case.receipts)} receipts")
    (out_dir / "fixtures.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")


if __name__ == "__main__":
    main(Path(sys.argv[1]))
