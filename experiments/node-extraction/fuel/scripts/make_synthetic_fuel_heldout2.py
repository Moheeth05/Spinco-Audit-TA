"""SECOND synthetic held-out set (heldout2): eight NEW layouts, written after the first held-out look and BEFORE the final extractor was
ever run on them. Same rules as the first generator: SYNTHETIC, invented, exact construction labels, robustness only - never evidence of
accuracy on authentic receipts. Re-uses the first generator's helpers/rendering (imported, not modified).

Several layouts are deliberately OUTSIDE the extractor's current vocabulary ('Net Payable', a column-header table, a receipt with no
pump name at all, brand-free card slip). They are here to measure what happens on unfamiliar wording: the right outcome is a null +
review, and a wrong confident value is the failure.

Usage: python make_synthetic_fuel_heldout2.py <out_dir> [--seed 7]
"""
import argparse, json, random, sys
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import make_synthetic_fuel as g  # noqa: E402  (frozen first generator, unchanged)


def t(pump, forms, d, amount, date_raw, amount_raw, review=False, why=None):
    return g.base_truth(pump, forms, d, amount, review=review, why=why, date_raw=date_raw, amount_raw=amount_raw)


def tpl_iocl_xtrareward(rng):
    dealer = rng.choice(g.DEALERS).upper()
    d, a = g.pick_date(rng), g.amount_for(rng)
    raw = f"{d.day:02d}-{g.MON[d.month - 1]}-{d.year % 100:02d}"
    lines = [("IndianOil", "c"), ("XTRAREWARDS", "c"), (dealer, "c"), (f"Date {raw}   {g.hms(rng)[:5]}", "l"), ("Product: XtraPremium", "l"), (f"Sale Amount : Rs.{a:,.2f}", "l"),
             (f"Reward Points Earned: {rng.randint(8, 120)}", "l"), (f"Points Balance: {rng.randint(500, 9000)}", "l"), ("Thank you", "c")]
    return lines, t(dealer.title(), ["INDIANOIL", "INDIAN OIL", "IOCL"], d, a, raw, f"Rs.{a:,.2f}")


def tpl_bpcl_fleet(rng):  # label vocabulary outside the extractor's list ('Net Payable')
    dealer = rng.choice(g.DEALERS).upper()
    d, a = g.pick_date(rng), g.amount_for(rng)
    lines = [("BHARAT PETROLEUM", "c"), (dealer, "c"), ("SMARTFLEET CARD SALE", "c"), (f"Date: {g.fmt_date(d, 'DD/MM/YYYY')}", "l"), ("Product: Diesel", "l"), (f"Net Payable : Rs. {a:,.2f}", "l"),
             (f"Card Balance : Rs. {rng.randint(2000, 90000):,}.00", "l")]
    return lines, t(dealer.title(), ["BHARAT PETROLEUM"], d, a, g.fmt_date(d, "DD/MM/YYYY"), f"Rs. {a:,.2f}")


def tpl_hp_table(rng):  # amount only in a column under a header row (label and value on different lines, other numbers beside it)
    dealer = rng.choice(g.DEALERS).upper()
    d, a = g.pick_date(rng), g.amount_for(rng)
    rate = rng.uniform(95, 108)
    lines = [("HP", "c"), (dealer, "c"), (f"Date: {g.fmt_date(d, 'DD-MM-YYYY')}", "l"), ("Product: Petrol", "l"), ("RATE      QTY      AMOUNT", "l"), (f"{rate:.2f}    {a / rate:.2f}    {a:.2f}", "l"), ("Thank you", "c")]
    return lines, t(dealer.title(), ["HP", "HINDUSTAN PETROLEUM"], d, a, g.fmt_date(d, "DD-MM-YYYY"), f"{a:.2f}")


def tpl_nayara_dealer_label(rng):
    dealer = rng.choice(g.DEALERS).upper()
    d, a = g.pick_date(rng), g.amount_for(rng)
    lines = [("NAYARA ENERGY", "c"), (f"Dealer: M/S {dealer}", "l"), (f"Date & Time : {d.isoformat()} {g.hms(rng)[:5]}", "l"), ("Fuel: Petrol", "l"), (f"Total Amount : {a:.2f}", "l"), ("Paid by: Cash", "l")]
    return lines, t(dealer.title(), ["NAYARA ENERGY", "NAYARA"], d, a, d.isoformat(), f"{a:.2f}")


def tpl_jiobp_upi(rng):  # same figure under two labels (corroboration) + a UPI reference full of digits
    dealer = rng.choice(g.DEALERS).upper()
    d, a = g.pick_date(rng), g.amount_for(rng)
    lines = [("jio-bp", "c"), (dealer, "c"), (f"{g.fmt_date(d, 'D MON YYYY')}", "l"), ("Product: Petrol", "l"), (f"Total Amount : Rs {a:.2f}", "l"), (f"Amount Paid : Rs {a:.2f}", "l"), (f"UPI Ref: {rng.randint(10**11, 10**12 - 1)}", "l")]
    return lines, t("jio-bp " + dealer.title(), ["JIO-BP", "JIO BP"], d, a, g.fmt_date(d, "D MON YYYY"), f"Rs {a:.2f}")


def tpl_shell_card_slip(rng):  # card-terminal wording; digits everywhere (batch, approval, invoice)
    dealer = rng.choice(g.DEALERS).upper()
    d, a = g.pick_date(rng, force_unambiguous=True), g.amount_for(rng)
    lines = [("SHELL", "c"), (dealer, "c"), ("CARD SALE", "c"), (f"DATE {g.fmt_date(d, 'DD/MM/YY')}  TIME {g.hms(rng)[:5]}", "l"), (f"CARD ****{rng.randint(1000, 9999)}", "l"), (f"BATCH {rng.randint(1, 99):03d}  INVOICE {rng.randint(1, 9999):06d}", "l"),
             (f"APPR CODE {rng.randint(100000, 999999)}", "l"), (f"SALE AMT: INR {a:,.2f}", "l"), ("PETROL", "l")]
    return lines, t("Shell " + dealer.title(), ["SHELL"], d, a, g.fmt_date(d, "DD/MM/YY"), f"INR {a:,.2f}")


def tpl_independent_no_header(rng):  # the pump name is simply not on the receipt: the only correct answer is null + review
    d, a = g.pick_date(rng), g.amount_for(rng)
    lines = [("SALE RECEIPT", "c"), (f"Date: {g.fmt_date(d, 'DD/MM/YYYY')}", "l"), ("Product: Diesel", "l"), (f"Amount: {a:.2f}", "l"), ("Thank you", "c")]
    return lines, t(None, [], d, a, g.fmt_date(d, "DD/MM/YYYY"), f"{a:.2f}", review=True, why="no pump name printed")


def tpl_reprint(rng):  # transaction date is ambiguous on its own, but a (later) print date in the same format proves the order
    dealer = rng.choice(g.DEALERS).upper()
    d, a = date(2026, rng.randint(5, 9), rng.randint(2, 11)), g.amount_for(rng)
    pd = date(d.year, d.month, rng.randint(22, 28))
    lines = [("HINDUSTAN PETROLEUM", "c"), (dealer, "c"), ("DUPLICATE", "c"), (f"Sale Date : {g.fmt_date(d, 'DD-MM-YYYY')}", "l"), (f"Reprinted : {g.fmt_date(pd, 'DD-MM-YYYY')}", "l"), (f"Net Amount : {a:.2f}", "l")]
    return lines, t(dealer.title(), ["HINDUSTAN PETROLEUM", "HPCL", "HP"], d, a, g.fmt_date(d, "DD-MM-YYYY"), f"{a:.2f}")


TEMPLATES2 = [("iocl_xtrareward", tpl_iocl_xtrareward), ("bpcl_fleet_netpayable", tpl_bpcl_fleet), ("hp_column_table", tpl_hp_table), ("nayara_dealer_label", tpl_nayara_dealer_label),
              ("jiobp_upi", tpl_jiobp_upi), ("shell_card_slip", tpl_shell_card_slip), ("independent_no_header", tpl_independent_no_header), ("reprint_proves_order", tpl_reprint)]


def build(out_dir: Path, seed: int):
    (out_dir / "images").mkdir(parents=True, exist_ok=True)
    records = []
    rng_master = random.Random(seed)
    for fam, fn in TEMPLATES2:
        for variant in range(6):
            rng = random.Random(f"{seed}-h2-{fam}-{variant}")
            lines, truth = fn(rng)
            degradation = ["clean", "clean", rng_master.choice(["faint", "blur"]), rng_master.choice(["skew", "noise"]), rng_master.choice(["lowres", "faint", "blur"]), rng_master.choice(["rotated90", "cropped_top", "skew"])][variant]
            img, deg = g.render(lines, rng, degradation, font_key=rng.choice(["cour", "consola", "lucon"]))
            if deg == "cropped_top":
                truth = g.Truth(truth, pump_name=None, pump_alternates=[], expect_review=True, review_reason="header cropped away: pump name absent")
            rid = f"syn2_heldout2_{fam}_{variant}"
            img.save(out_dir / "images" / f"{rid}.png")
            records.append({"receipt_id": rid, "source_type": "synthetic", "source_reference": "make_synthetic_fuel_heldout2.py (invented)", "receipt_family": fam, "split": "heldout2",
                            "difficulty": deg, "kind": "fuel", "truth": dict(truth), "annotation_status": "construction_truth"})
    (out_dir / "synthetic2_annotations.json").write_text(json.dumps({"seed": seed, "note": "SYNTHETIC heldout2 - construction labels only.", "records": records}, indent=1), encoding="utf-8")
    print(f"wrote {len(records)} heldout2 images -> {out_dir}")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("out_dir")
    ap.add_argument("--seed", type=int, default=7)
    a = ap.parse_args()
    build(Path(a.out_dir), a.seed)
