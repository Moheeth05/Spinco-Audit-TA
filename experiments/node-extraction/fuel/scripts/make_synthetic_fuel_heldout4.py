"""FOURTH synthetic held-out set (heldout4): fresh layouts for the priority brands - Nayara, Jio-bp, Shell and independent stations - created because heldout3 was
EXPOSED during this phase (the latency investigation printed two of its receipt-level outcomes, and the edge-truncation fix was made after seeing one of them), so
heldout3 is development data from that point on. heldout4 is generated and frozen BEFORE the final extractor and OCR code run on it (reports/FROZEN_BEFORE_HELDOUT4_FIRST_RUN.json).
SYNTHETIC: invented layouts/transactions with exact construction labels; robustness only, never evidence of accuracy on authentic receipts.

Usage: python make_synthetic_fuel_heldout4.py <out_dir> [--seed 29]
"""
import argparse, json, random, sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import make_synthetic_fuel as g  # noqa: E402  (frozen first generator, unchanged)


def t(pump, forms, d, amount, date_raw, amount_raw, review=False, why=None):
    return g.base_truth(pump, forms, d, amount, review=review, why=why, date_raw=date_raw, amount_raw=amount_raw)


def tpl_nayara_a(rng):
    dealer = rng.choice(g.DEALERS).upper()
    d, a = g.pick_date(rng), g.amount_for(rng)
    raw = g.fmt_date(d, "DD-MON-YYYY")
    lines = [("NAYARA ENERGY", "c"), ("(Authorised Dealer)", "c"), (dealer, "c"), (f"Sale No : {rng.randint(10000, 99999)}", "l"), (f"Date : {raw} {g.hms(rng)[:5]}", "l"), ("Product : Petrol", "l"),
             (f"Quantity : {a / rng.uniform(95, 105):.2f} L", "l"), (f"Sale Amount (INR) : {a:,.2f}", "l"), ("Visit again", "c")]
    return lines, t(dealer.title(), ["NAYARA ENERGY", "NAYARA"], d, a, raw, f"{a:,.2f}")


def tpl_nayara_upi(rng):
    dealer = rng.choice(g.DEALERS).upper()
    d, a = g.pick_date(rng), g.amount_for(rng)
    lines = [("Nayara Energy", "c"), (f"Fuel Station: {dealer}", "l"), (f"Date: {d.isoformat()}", "l"), (f"UPI Txn: {rng.randint(10**11, 10**12 - 1)}", "l"), (f"Paid: Rs {a:,.2f}", "l"), (f"Total: {a:,.2f}", "l")]
    return lines, t(dealer.title(), ["NAYARA ENERGY", "NAYARA"], d, a, d.isoformat(), f"{a:,.2f}")


def tpl_jiobp_a(rng):
    dealer = rng.choice(g.DEALERS).upper()
    d, a = g.pick_date(rng), g.amount_for(rng)
    raw = f"{g.fmt_date(d, 'DD/MM/YYYY')} {g.hms(rng)}"
    lines = [("jio-bp", "c"), (f"{dealer} RO", "c"), (f"Date/Time {raw}", "l"), ("Fuel: Diesel", "l"), (f"Rate {rng.uniform(88, 100):.2f}", "l"), (f"Fuel Total Rs {a:.2f}", "l")]
    return lines, t("jio-bp " + dealer.title() + " RO", ["JIO-BP", "JIO BP"], d, a, g.fmt_date(d, "DD/MM/YYYY"), f"Rs {a:.2f}")


def tpl_jiobp_b(rng):
    dealer = rng.choice(g.DEALERS)
    d, a = g.pick_date(rng), g.amount_for(rng)
    lines = [("jio-bp mobility", "c"), (dealer, "c"), (f"Date : {g.fmt_date(d, 'DD-MM-YYYY')}", "l"), ("Product : Petrol", "l"), (f"CGST 0.00   SGST 0.00", "l"), (f"Reward Pts : {rng.randint(5, 90)}", "l"), (f"Sale Amount : ₹ {a:,.2f}", "l")]
    return lines, t("jio-bp " + dealer.title(), ["JIO-BP", "JIO BP"], d, a, g.fmt_date(d, "DD-MM-YYYY"), f"₹ {a:,.2f}")


def tpl_shell_a(rng):
    dealer = rng.choice(g.DEALERS).upper()
    d, a = g.pick_date(rng), g.amount_for(rng)
    raw = g.fmt_date(d, "D MON YYYY").title()
    lines = [("SHELL", "c"), ("Shell Retail Pvt Ltd", "c"), (dealer, "c"), (f"Date: {raw}", "l"), ("Grade: FuelSave Petrol", "l"), (f"Amount: {a:,.2f}", "l")]
    return lines, t("Shell " + dealer.title(), ["SHELL"], d, a, raw, f"{a:,.2f}")


def tpl_shell_b(rng):
    dealer = rng.choice(g.DEALERS).upper()
    d, a = g.pick_date(rng), g.amount_for(rng)
    lines = [("Shell Select Station", "c"), (dealer, "c"), (f"Transaction : {g.fmt_date(d, 'DD/MM/YYYY')}", "l"), ("Fuel: Diesel", "l"), (f"Loyalty balance: {rng.randint(100, 9000)}", "l"), (f"Total (INR) {a:,.2f}", "l")]
    return lines, t("Shell " + dealer.title(), ["SHELL", "SHELL SELECT STATION"], d, a, g.fmt_date(d, "DD/MM/YYYY"), f"{a:,.2f}")


def tpl_independent_a(rng):
    dealer = rng.choice(g.DEALERS).upper()
    d, a = g.pick_date(rng), g.amount_for(rng)
    lines = [(f"{dealer} PETROL PUMP", "c"), (f"Bill No : {rng.randint(100, 9999)}", "l"), (f"Date {g.fmt_date(d, 'DD-MM-YYYY')}", "l"), ("Diesel", "l"), (f"Net Amount {a:.2f}", "l")]
    return lines, t(dealer.title() + " PETROL PUMP", [dealer + " PETROL PUMP", dealer], d, a, g.fmt_date(d, "DD-MM-YYYY"), f"{a:.2f}")


def tpl_independent_ambiguous(rng):  # no evidence of day/month order anywhere: the date must stay unresolved
    dealer = rng.choice(g.DEALERS).upper()
    d = date(2026, rng.randint(5, 9), rng.randint(2, 11))
    if d.day == d.month:
        d = d.replace(day=d.day + 1)
    a = g.amount_for(rng)
    lines = [(f"SRI {dealer.split()[0]} FUEL STATION", "c"), (f"Date: {g.fmt_date(d, 'DD/MM/YYYY')}", "l"), ("Petrol", "l"), (f"Sale: Rs {a:,.2f}", "l")]
    return lines, t(f"SRI {dealer.split()[0]} FUEL STATION".title(), [f"SRI {dealer.split()[0]} FUEL STATION"], None, a, g.fmt_date(d, "DD/MM/YYYY"), f"Rs {a:,.2f}", review=True, why="ambiguous day/month order")


TEMPLATES4 = [("nayara_authorised_dealer", tpl_nayara_a), ("nayara_upi_paid", tpl_nayara_upi), ("jiobp_ro_fueltotal", tpl_jiobp_a), ("jiobp_mobility_rewards", tpl_jiobp_b),
              ("shell_retail_grade", tpl_shell_a), ("shell_select_loyalty", tpl_shell_b), ("independent_petrol_pump", tpl_independent_a), ("independent_ambiguous_date", tpl_independent_ambiguous)]


def build(out_dir: Path, seed: int):
    (out_dir / "images").mkdir(parents=True, exist_ok=True)
    records = []
    rng_master = random.Random(seed)
    for fam, fn in TEMPLATES4:
        for variant in range(6):
            rng = random.Random(f"{seed}-h4-{fam}-{variant}")
            lines, truth = fn(rng)
            degradation = ["clean", "clean", rng_master.choice(["faint", "blur"]), rng_master.choice(["skew", "noise"]), rng_master.choice(["lowres", "faint", "blur"]), rng_master.choice(["rotated90", "cropped_top", "skew"])][variant]
            img, deg = g.render(lines, rng, degradation, font_key=rng.choice(["cour", "consola", "lucon"]))
            if deg == "cropped_top":
                truth = g.Truth(truth, pump_name=None, pump_alternates=[], expect_review=True, review_reason="header cropped away: pump name absent")
            rid = f"syn4_heldout4_{fam}_{variant}"
            img.save(out_dir / "images" / f"{rid}.png")
            records.append({"receipt_id": rid, "source_type": "synthetic", "source_reference": "make_synthetic_fuel_heldout4.py (invented)", "receipt_family": fam, "split": "heldout4",
                            "difficulty": deg, "kind": "fuel", "truth": dict(truth), "annotation_status": "construction_truth"})
    (out_dir / "synthetic4_annotations.json").write_text(json.dumps({"seed": seed, "note": "SYNTHETIC heldout4 - construction labels only.", "records": records}, indent=1), encoding="utf-8")
    print(f"wrote {len(records)} heldout4 images -> {out_dir}")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("out_dir")
    ap.add_argument("--seed", type=int, default=29)
    a = ap.parse_args()
    build(Path(a.out_dir), a.seed)
