"""THIRD synthetic held-out set (heldout3), aimed at the priority brands for which no authentic receipt is available: Nayara, Jio-bp, Shell and
independent stations. SYNTHETIC: invented layouts and transactions with exact construction labels - robustness testing only, never evidence of accuracy
on authentic receipts. Written BEFORE the phase's final extractor/OCR code was run on it; its generator hash and the code hashes are frozen in
reports/FROZEN_BEFORE_HELDOUT3_FIRST_RUN.json at that moment. Some wording is deliberately outside the extractor's vocabulary ('Total Value',
'Total Payable', DD.MM.YY dates): the right outcome there is null + review.

Usage: python make_synthetic_fuel_heldout3.py <out_dir> [--seed 13]
"""
import argparse, json, random, sys
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import make_synthetic_fuel as g  # noqa: E402  (frozen first generator, unchanged)


def t(pump, forms, d, amount, date_raw, amount_raw, review=False, why=None):
    return g.base_truth(pump, forms, d, amount, review=review, why=why, date_raw=date_raw, amount_raw=amount_raw)


def dotted(d, yy=False):
    return f"{d.day:02d}.{d.month:02d}.{d.year % 100:02d}" if yy else f"{d.day:02d}.{d.month:02d}.{d.year}"


def tpl_nayara_outlet(rng):
    dealer = rng.choice(g.DEALERS).upper()
    d, a = g.pick_date(rng), g.amount_for(rng)
    lines = [("NAYARA ENERGY LIMITED", "c"), (f"Retail Outlet : {dealer}", "l"), (f"Bill No : NY{rng.randint(10**6, 10**7 - 1)}", "l"), (f"Date : {dotted(d)}   Time : {g.hms(rng)[:5]}", "l"), ("Fuel : Diesel", "l"),
             (f"Qty : {a / rng.uniform(88, 100):.2f} Ltr", "l"), (f"Total Value : Rs {a:,.2f}", "l"), ("Thank you", "c")]
    return lines, t(dealer.title(), ["NAYARA ENERGY", "NAYARA"], d, a, dotted(d), f"Rs {a:,.2f}")


def tpl_nayara_card(rng):
    dealer = rng.choice(g.DEALERS).upper()
    d, a = g.pick_date(rng), g.amount_for(rng)
    lines = [("Nayara Energy", "c"), (dealer, "c"), ("CARD SALE", "c"), (f"Txn Date : {g.fmt_date(d, 'DD/MM/YYYY')} {g.hms(rng)[:5]}", "l"), (f"Card : XXXX XXXX XXXX {rng.randint(1000, 9999)}", "l"), (f"Auth Code : {rng.randint(100000, 999999)}", "l"),
             (f"Amount : INR {a:,.2f}", "l")]
    return lines, t(dealer.title(), ["NAYARA ENERGY", "NAYARA"], d, a, g.fmt_date(d, "DD/MM/YYYY"), f"INR {a:,.2f}")


def tpl_jiobp_pulse(rng):  # 'Total Payable' is outside the extractor's labelled vocabulary
    dealer = rng.choice(g.DEALERS)
    d, a = g.pick_date(rng), g.amount_for(rng)
    raw = f"{d.day:02d}-{g.MON[d.month - 1].title()}-{d.year}"
    lines = [("jio-bp pulse", "c"), (f"Station: {dealer}", "l"), (f"Invoice Date: {raw}", "l"), ("Product: Petrol", "l"), (f"Total Payable: Rs {a:,.2f}", "l"), (f"Reward points: {rng.randint(5, 80)}", "l")]
    return lines, t(dealer.title(), ["JIO-BP", "JIO BP"], d, a, raw, f"Rs {a:,.2f}")


def tpl_jiobp_fleet(rng):
    dealer = rng.choice(g.DEALERS).upper()
    d, a = g.pick_date(rng), g.amount_for(rng)
    lines = [("jio-bp", "c"), (dealer, "c"), ("FLEET CARD SALE", "c"), (f"{g.fmt_date(d, 'D MON YYYY')}  {g.hms(rng)[:5]}", "l"), ("Product: Diesel", "l"), (f"Net Amt : {a:,.2f}", "l"), (f"Odometer : {rng.randint(10000, 99999)}", "l")]
    return lines, t("jio-bp " + dealer.title(), ["JIO-BP", "JIO BP"], d, a, g.fmt_date(d, "D MON YYYY"), f"{a:,.2f}")


def tpl_shell_retail(rng):
    dealer = rng.choice(g.DEALERS).upper()
    d, a = g.pick_date(rng), g.amount_for(rng)
    lines = [("Shell Retail India", "c"), (f"Station : {dealer}", "l"), (f"Date {g.fmt_date(d, 'DD/MM/YYYY')}  {g.hms(rng)[:5]}", "l"), ("V-Power Petrol", "l"), (f"Fuel Sale Rs. {a:,.2f}", "l"), ("Drive safe", "c")]
    return lines, t(dealer.title(), ["SHELL", "SHELL RETAIL INDIA"], d, a, g.fmt_date(d, "DD/MM/YYYY"), f"Rs. {a:,.2f}")


def tpl_shell_loyalty(rng):
    dealer = rng.choice(g.DEALERS).upper()
    d, a = g.pick_date(rng), g.amount_for(rng)
    lines = [("SHELL", "c"), (dealer, "c"), (f"{g.fmt_date(d, 'DD-MM-YYYY')} {g.hms(rng)[:5]}", "l"), ("Product: Diesel", "l"), (f"ClubSmart points earned : {rng.randint(10, 150)}", "l"), (f"ClubSmart balance : {rng.randint(100, 9000)}", "l"), (f"TOTAL {a:,.2f}", "l")]
    return lines, t("Shell " + dealer.title(), ["SHELL"], d, a, g.fmt_date(d, "DD-MM-YYYY"), f"{a:,.2f}")


def tpl_independent_cashmemo(rng):
    dealer = rng.choice(g.DEALERS)
    d, a = g.pick_date(rng), float(rng.choice([500, 800, 1000, 1500, 2000, 2500, 3000]))
    lines = [(f"M/s {dealer}", "c"), ("Petrol & Diesel Dealer", "c"), (f"GST {rng.randint(10, 37)}ABCDE{rng.randint(1000, 9999)}F1Z{rng.randint(1, 9)}", "c"), ("CASH MEMO", "c"), (f"Memo No. {rng.randint(1000, 9999)}", "l"),
             (f"Dt: {g.fmt_date(d, 'DD-MM-YY')}", "l"), ("Diesel", "l"), (f"Amount Rs {int(a)}/-", "l"), ("E & OE", "l")]
    return lines, t(dealer.title(), [], d, a, g.fmt_date(d, "DD-MM-YY"), f"Rs {int(a)}/-")


def tpl_independent_short_year_dotted(rng):  # DD.MM.YY dotted 2-digit year: the extractor refuses it by design -> expect null date + review
    dealer = rng.choice(g.DEALERS).upper()
    d, a = g.pick_date(rng), g.amount_for(rng)
    lines = [(dealer, "c"), ("PETROL PUMP", "c"), (f"Date {dotted(d, yy=True)}   {g.hms(rng)[:5]}", "l"), ("Product: Petrol", "l"), (f"Amount : {a:.2f}", "l")]
    return lines, t(dealer.title(), [], d, a, dotted(d, yy=True), f"{a:.2f}", review=True, why="dotted 2-digit-year date is not read by design")


TEMPLATES3 = [("nayara_outlet_totalvalue", tpl_nayara_outlet), ("nayara_card_sale", tpl_nayara_card), ("jiobp_pulse_totalpayable", tpl_jiobp_pulse), ("jiobp_fleet_netamt", tpl_jiobp_fleet),
              ("shell_retail_fuelsale", tpl_shell_retail), ("shell_loyalty_decoys", tpl_shell_loyalty), ("independent_cashmemo", tpl_independent_cashmemo), ("independent_dotted_short_year", tpl_independent_short_year_dotted)]


def build(out_dir: Path, seed: int):
    (out_dir / "images").mkdir(parents=True, exist_ok=True)
    records = []
    rng_master = random.Random(seed)
    for fam, fn in TEMPLATES3:
        for variant in range(6):
            rng = random.Random(f"{seed}-h3-{fam}-{variant}")
            lines, truth = fn(rng)
            degradation = ["clean", "clean", rng_master.choice(["faint", "blur"]), rng_master.choice(["skew", "noise"]), rng_master.choice(["lowres", "faint", "blur"]), rng_master.choice(["rotated90", "cropped_top", "skew"])][variant]
            img, deg = g.render(lines, rng, degradation, font_key=rng.choice(["cour", "consola", "lucon"]))
            if deg == "cropped_top":
                truth = g.Truth(truth, pump_name=None, pump_alternates=[], expect_review=True, review_reason="header cropped away: pump name absent")
            rid = f"syn3_heldout3_{fam}_{variant}"
            img.save(out_dir / "images" / f"{rid}.png")
            records.append({"receipt_id": rid, "source_type": "synthetic", "source_reference": "make_synthetic_fuel_heldout3.py (invented)", "receipt_family": fam, "split": "heldout3",
                            "difficulty": deg, "kind": "fuel", "truth": dict(truth), "annotation_status": "construction_truth"})
    (out_dir / "synthetic3_annotations.json").write_text(json.dumps({"seed": seed, "note": "SYNTHETIC heldout3 - construction labels only.", "records": records}, indent=1), encoding="utf-8")
    print(f"wrote {len(records)} heldout3 images -> {out_dir}")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("out_dir")
    ap.add_argument("--seed", type=int, default=13)
    a = ap.parse_args()
    build(Path(a.out_dir), a.seed)
