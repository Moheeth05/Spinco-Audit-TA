"""Synthetic Indian fuel-pump receipts (+ non-fuel controls) rendered as images, with EXACT construction labels.

THESE ARE SYNTHETIC. They are invented layouts/transactions, labelled by construction. They test robustness of the
three target fields (pump_name, transaction_date, total_amount) and must never be reported as evidence of accuracy on
authentic receipts. Layouts were designed from (a) the public generator sites' sample layouts (OnlineBiller, BillCopy -
see sources.json) and (b) the structure of authentic receipts already in the project's approved reports. No logo or brand
artwork is reproduced; brand names appear only as printed text.

Receipt FAMILIES are split dev / heldout by layout template, never by receipt. Held-out templates were written down
(frozen) before they were ever run through the extractor.

Usage: python make_synthetic_fuel.py <out_dir> [--seed 1]
Writes <out_dir>/images/*.png and <out_dir>/synthetic_annotations.json
"""
import argparse, json, random
from datetime import date, timedelta
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter, ImageFont, ImageOps

FONTS = {"cour": r"C:\Windows\Fonts\cour.ttf", "consola": r"C:\Windows\Fonts\consola.ttf", "lucon": r"C:\Windows\Fonts\lucon.ttf",
         "arial": r"C:\Windows\Fonts\arial.ttf", "ink": r"C:\Windows\Fonts\Inkfree.ttf"}
MON = ["JAN", "FEB", "MAR", "APR", "MAY", "JUN", "JUL", "AUG", "SEP", "OCT", "NOV", "DEC"]

DEALERS = ["Sri Venkateshwara Auto Fuels", "Gurunanak Filling Station", "Maa Tara Service Station", "Patel Petroleum Point", "Khanna Fuel Centre",
           "Annapurna Petro Services", "Jai Hind Auto Fuels", "Shree Ganesh Petroleum", "Rajdhani Filling Point", "Lakshmi Fuel Station",
           "Balaji Petroleum Service", "Hari Om Auto Care Fuels", "Sagar Fuel Point", "Kaveri Service Station", "Amba Petro Centre"]
CITIES = ["Ahmedabad", "Pune", "Nashik", "Vijayawada", "Coimbatore", "Jaipur", "Indore", "Lucknow", "Kochi", "Bhubaneswar", "Surat", "Nagpur"]


def fmt_amount(a: float, style: str) -> str:
    if style == "plain":
        return f"{a:.2f}"
    if style == "comma":
        return f"{a:,.2f}"
    if style == "rs":
        return f"Rs. {a:,.2f}"
    if style == "rupee":
        return f"\u20b9 {a:,.2f}"
    if style == "inr":
        return f"{a:.2f} INR"
    if style == "slash":
        return f"Rs {int(a)}/-"
    raise ValueError(style)


def fmt_date(d: date, style: str) -> str:
    return {
        "DD/MM/YYYY": f"{d.day:02d}/{d.month:02d}/{d.year}",
        "DD-MM-YYYY": f"{d.day:02d}-{d.month:02d}-{d.year}",
        "DD-MM-YY": f"{d.day:02d}-{d.month:02d}-{d.year % 100:02d}",
        "DD/MM/YY": f"{d.day:02d}/{d.month:02d}/{d.year % 100:02d}",
        "DD-MON-YYYY": f"{d.day:02d}-{MON[d.month - 1]}-{d.year}",
        "D MON YYYY": f"{d.day} {MON[d.month - 1]} {d.year}",
        "ISO": d.isoformat(),
        "MM/DD/YYYY": f"{d.month:02d}/{d.day:02d}/{d.year}",
    }[style]


class Truth(dict):
    pass


def base_truth(pump, alternates, d, amount, review=False, why=None, date_raw=None, amount_raw=None):
    return Truth(pump_name=pump, pump_alternates=alternates, date=d.isoformat() if d else None, amount=amount, expect_review=review,
                 review_reason=why, date_raw=date_raw, amount_raw=amount_raw)


def pick_date(rng, force_unambiguous=True):
    month = rng.choice([5, 6, 7, 8, 9])
    day = rng.randint(13, 28) if force_unambiguous else rng.randint(1, 12)
    return date(2026, month, day)


def amount_for(rng):
    return float(rng.choice([300, 500, 800, 1000, 1200, 1500, 1800, 2000, 2500, 3000, 3500, 4000, 4500])) + rng.choice([0.0, 0.0, 0.5, 0.66, 0.34, 0.12])


def hms(rng):
    return f"{rng.randint(6, 21):02d}:{rng.randint(0, 59):02d}:{rng.randint(0, 59):02d}"


# ---------------------------------------------------------------------------------------------- receipt templates
# each returns (lines, truth, style) ; lines = list of (text, align) with align in {"c","l"}


def tpl_iocl_thermal(rng):  # DEV
    dealer = rng.choice(DEALERS).upper()
    d, a = pick_date(rng), amount_for(rng)
    dstyle = rng.choice(["DD/MM/YYYY", "DD-MM-YYYY"])
    lines = [("INDIAN OIL CORPORATION LTD", "c"), (f"({dealer})", "c"), (f"{rng.choice(CITIES).upper()} - {rng.randint(300000, 799999)}", "c"), ("CASH MEMO", "c"), ("-" * 30, "l"),
             (f"BILL NO: {rng.randint(1000, 99999)}", "l"), (f"DATE: {fmt_date(d, dstyle)}  TIME: {hms(rng)}", "l"), ("PRODUCT: PETROL", "l"),
             (f"RATE(Rs/Ltr): {rng.uniform(95, 108):.2f}", "l"), (f"VOLUME(Ltr): {a / 100:.2f}", "l"), (f"AMOUNT(Rs): {a:.2f}", "l"), ("-" * 30, "l"), ("THANK YOU, VISIT AGAIN", "c")]
    return lines, base_truth(dealer.title(), ["INDIAN OIL", "IOCL"], d, a, date_raw=fmt_date(d, dstyle), amount_raw=f"{a:.2f}")


def tpl_hpcl_pos(rng):  # DEV   (modelled on the structure of an authentic HP receipt: labels row, values row)
    dealer = rng.choice(DEALERS).upper()
    d, a = pick_date(rng), amount_for(rng)
    lines = [("HP", "c"), (dealer, "c"), (rng.choice(CITIES).upper(), "c"), (f"Bill No : Jul-{rng.randint(100000, 999999)}-ORGNL", "l"), (f"Trns.ID : {rng.randint(10**15, 10**16 - 1)}", "l"),
             (f"Vehi.No : GJ{rng.randint(10, 39)}AB{rng.randint(1000, 9999)}", "l"), (f"Date : {fmt_date(d, 'DD/MM/YYYY')}", "l"), (f"Time : {hms(rng)}", "l"), ("Physical Receipt", "l"),
             (f"Rate : Rs. {rng.uniform(95, 108):.2f}", "l"), (f"Sale : Rs. {a:.2f}", "l"), (f"Volume : {a / 100:.2f} L", "l")]
    return lines, base_truth(dealer.title(), ["HP", "HINDUSTAN PETROLEUM", "HPCL"], d, a, date_raw=fmt_date(d, "DD/MM/YYYY"), amount_raw=f"{a:.2f}")


def tpl_bpcl_txn(rng):  # DEV
    dealer = rng.choice(DEALERS).upper()
    d, a = pick_date(rng), amount_for(rng)
    lines = [("BHARAT PETROLEUM", "c"), (dealer, "c"), (f"Date: {fmt_date(d, 'DD-MM-YYYY')}", "l"), (f"Time: {hms(rng)}", "l"), (f"Txn Id: {rng.randint(10**9, 10**10 - 1)}", "l"),
             (f"TxSt: {fmt_date(d, 'DD-MM-YY')} {hms(rng)}", "l"), (f"TxEnd: {fmt_date(d, 'DD-MM-YY')} {hms(rng)}", "l"), (f"Rate/Ltr. : {rng.uniform(95, 108):.2f}", "l"),
             (f"Volume(Ltr.): {a / 100:.2f}", "l"), (f"Amount (Rs.) : {a:.2f}", "l"), ("PayMode : CREDIT CARD", "l"), ("THANKS FOR FUELING VISIT AGAIN", "c")]
    return lines, base_truth(dealer.title(), ["BHARAT PETROLEUM", "BPCL"], d, a, date_raw=fmt_date(d, "DD-MM-YYYY"), amount_raw=f"{a:.2f}")


def tpl_independent_memo(rng):  # DEV
    dealer = rng.choice(DEALERS).upper()
    d, a = pick_date(rng), amount_for(rng)
    lines = [(f"M/s {dealer}", "c"), (f"Dealer: Indian Oil", "c"), (f"GSTIN: 24ABCDE{rng.randint(1000, 9999)}F1Z5", "c"), ("CASH BILL", "c"), (f"Date {fmt_date(d, 'DD-MM-YY')}", "l"), (f"Vehicle {rng.choice(['Car', 'Bike'])}", "l"),
             ("Diesel", "l"), (f"Total {fmt_amount(a, 'slash') if a == int(a) else fmt_amount(a, 'rs')}", "l"), ("Signature", "l")]
    return lines, base_truth(dealer.title(), ["INDIAN OIL"], d, a, date_raw=fmt_date(d, "DD-MM-YY"), amount_raw=fmt_amount(a, "slash") if a == int(a) else fmt_amount(a, "rs"))


def tpl_faint_generic(rng):  # DEV (no brand at all)
    dealer = rng.choice(DEALERS).upper()
    d, a = pick_date(rng), amount_for(rng)
    lines = [(dealer, "c"), ("SALE RECEIPT", "c"), (f"DATE : {fmt_date(d, 'DD/MM/YYYY')}", "l"), (f"TIME : {hms(rng)}", "l"), ("FUEL : DIESEL", "l"), (f"AMOUNT : {a:.2f}", "l"), ("THANK YOU", "c")]
    return lines, base_truth(dealer.title(), [], d, a, date_raw=fmt_date(d, "DD/MM/YYYY"), amount_raw=f"{a:.2f}")


# ---- held-out templates (frozen before first extractor run)


def tpl_nayara(rng):  # HELDOUT
    dealer = rng.choice(DEALERS).upper()
    d, a = pick_date(rng), amount_for(rng)
    lines = [("NAYARA ENERGY LIMITED", "c"), ("(formerly Essar Oil)", "c"), (dealer, "c"), (f"Invoice No : {rng.randint(10**6, 10**7 - 1)}", "l"), (f"Sale Date : {fmt_date(d, 'DD-MON-YYYY')}", "l"),
             (f"Sale Time : {hms(rng)}", "l"), ("Fuel : Diesel", "l"), (f"Unit Price : {rng.uniform(88, 100):.2f}", "l"), (f"SALE AMOUNT : {a:.2f}", "l"), (f"Loyalty Points Earned : {rng.randint(5, 90)}", "l"),
             (f"Points Balance : {rng.randint(100, 9000)}", "l"), ("Thank you", "c")]
    return lines, base_truth(dealer.title(), ["NAYARA ENERGY", "NAYARA"], d, a, date_raw=fmt_date(d, "DD-MON-YYYY"), amount_raw=f"{a:.2f}")


def tpl_jiobp(rng):  # HELDOUT
    dealer = rng.choice(DEALERS).upper()
    d, a = pick_date(rng), amount_for(rng)
    lines = [("jio-bp", "c"), (f"{dealer} - {rng.choice(CITIES)}", "c"), ("TAX INVOICE", "c"), (f"Order #{rng.randint(10**7, 10**8 - 1)}", "l"), (f"{fmt_date(d, 'D MON YYYY')} {hms(rng)[:5]}", "l"),
             ("Product : Petrol", "l"), (f"Price per Ltr : {rng.uniform(95, 108):.2f}", "l"), (f"Total Amount : {fmt_amount(a, 'rupee')}", "l"), (f"Reward points: {rng.randint(5, 90)}", "l"), ("Paid via UPI", "l")]
    return lines, base_truth("jio-bp " + dealer.title(), ["JIO-BP", "JIO BP"], d, a, date_raw=fmt_date(d, "D MON YYYY"), amount_raw=fmt_amount(a, "rupee"))


def tpl_shell(rng):  # HELDOUT
    dealer = rng.choice(DEALERS).upper()
    d, a = pick_date(rng), amount_for(rng)
    lines = [("Shell", "c"), ("Shell India Markets Pvt Ltd", "c"), (dealer, "c"), (f"Transaction Date : {fmt_date(d, 'ISO')}", "l"), (f"Transaction Time : {hms(rng)}", "l"), ("Product : V-Power", "l"),
             (f"Total (INR) : {fmt_amount(a, 'comma')}", "l"), (f"Card Balance : {rng.randint(500, 40000):,}.00", "l"), ("Approved", "c")]
    return lines, base_truth("Shell " + dealer.title(), ["SHELL"], d, a, date_raw=fmt_date(d, "ISO"), amount_raw=fmt_amount(a, "comma"))


def tpl_generator_style(rng):  # HELDOUT (glued date/time, currency suffix: the style shown by public receipt generators)
    dealer = rng.choice(DEALERS)
    d, a = pick_date(rng), amount_for(rng)
    raw = f"{d.day:02d}-{MON[d.month - 1]}-{d.year}{hms(rng)}"
    lines = [("Bharat Petroleum Corporation", "c"), (dealer, "c"), ("ORIGINAL", "c"), ("*" * 28, "l"), (f"TXN NO: {rng.randint(10**9, 10**10 - 1)}", "l"), (raw, "l"), (f"INVOICE NO: {rng.randint(10000, 99999)}", "l"),
             ("PRODUCT: PETROL", "l"), (f"RATE: {rng.uniform(95, 108):.2f} INR/Ltr", "l"), (f"AMOUNT: {a:.2f} INR", "l"), ("*" * 28, "l"), ("Thank You! Visit Again.", "c")]
    return lines, base_truth(dealer.title(), ["BHARAT PETROLEUM", "BPCL"], d, a, date_raw=raw[:11], amount_raw=f"{a:.2f} INR")


def tpl_print_vs_txn(rng):  # HELDOUT (two dates: transaction date and a later print date)
    dealer = rng.choice(DEALERS).upper()
    d, a = pick_date(rng), amount_for(rng)
    pd = d + timedelta(days=1)
    lines = [("HINDUSTAN PETROLEUM", "c"), (dealer, "c"), (f"Transaction Date : {fmt_date(d, 'DD/MM/YYYY')}", "l"), (f"Printed On : {fmt_date(pd, 'DD/MM/YYYY')} {hms(rng)}", "l"), ("Duplicate Copy", "l"),
             (f"Net Amount : {a:.2f}", "l"), ("Thank you", "c")]
    return lines, base_truth(dealer.title(), ["HINDUSTAN PETROLEUM", "HPCL", "HP"], d, a, date_raw=fmt_date(d, "DD/MM/YYYY"), amount_raw=f"{a:.2f}")


def tpl_ambiguous_date(rng):  # HELDOUT (no cue anywhere: day/month order unprovable -> must be review, no normalized date)
    dealer = rng.choice(DEALERS).upper()
    d = date(2026, rng.randint(5, 9), rng.randint(2, 11))  # both components <= 12, and day != month
    if d.day == d.month:
        d = d.replace(day=d.day + 1)
    a = amount_for(rng)
    lines = [("INDIANOIL", "c"), (dealer, "c"), (f"Date : {fmt_date(d, 'DD/MM/YYYY')}", "l"), (f"Time : {hms(rng)}", "l"), (f"Amount : {a:.2f}", "l")]
    return lines, base_truth(dealer.title(), ["INDIANOIL", "INDIAN OIL"], None, a, review=True, why="ambiguous day/month order", date_raw=fmt_date(d, "DD/MM/YYYY"), amount_raw=f"{a:.2f}")


def tpl_label_split(rng):  # HELDOUT (labels and values on separate lines, as column-split OCR produces)
    dealer = rng.choice(DEALERS).upper()
    d, a = pick_date(rng), amount_for(rng)
    lines = [("IOCL", "c"), (dealer, "c"), ("Date", "l"), (fmt_date(d, "DD/MM/YYYY"), "l"), ("Time", "l"), (hms(rng), "l"), ("Rate", "l"), (f"{rng.uniform(95, 108):.2f}", "l"), ("Sale Amount", "l"), (f"{a:.2f}", "l")]
    return lines, base_truth(dealer.title(), ["IOCL", "INDIAN OIL"], d, a, date_raw=fmt_date(d, "DD/MM/YYYY"), amount_raw=f"{a:.2f}")


def tpl_conflicting_annotation(rng):  # HELDOUT (a hand-written-looking number beside the printed amount: conflicting evidence -> review)
    dealer = rng.choice(DEALERS).upper()
    d, a = pick_date(rng), amount_for(rng)
    other = a + rng.choice([100, 200, 500])
    lines = [("BPCL", "c"), (dealer, "c"), (f"Date: {fmt_date(d, 'DD-MM-YYYY')}", "l"), (f"AMOUNT : {a:.2f}", "l"), (f"@@HAND:{int(other)}", "l")]
    return lines, base_truth(dealer.title(), ["BPCL"], d, a, review=True, why="handwritten number conflicts with printed amount", date_raw=fmt_date(d, "DD-MM-YYYY"), amount_raw=f"{a:.2f}")


TEMPLATES = {
    "dev": [("iocl_thermal", tpl_iocl_thermal), ("hpcl_pos", tpl_hpcl_pos), ("bpcl_txn", tpl_bpcl_txn), ("independent_memo", tpl_independent_memo), ("faint_generic", tpl_faint_generic)],
    "heldout": [("nayara", tpl_nayara), ("jiobp", tpl_jiobp), ("shell", tpl_shell), ("generator_style", tpl_generator_style), ("print_vs_txn", tpl_print_vs_txn),
                ("ambiguous_date", tpl_ambiguous_date), ("label_split", tpl_label_split), ("conflicting_annotation", tpl_conflicting_annotation)],
}

# ---------------------------------------------------------------------------------------------- non-fuel generic controls


def ctl_restaurant(rng):
    d, a = pick_date(rng), float(rng.choice([450, 685, 920, 1340]))
    return [("ADYAR SWEETS & CAFE", "c"), (f"Bill No {rng.randint(1000, 9999)}", "l"), (f"Date: {fmt_date(d, 'DD/MM/YYYY')}", "l"), ("Masala Dosa x2", "l"), ("Filter Coffee x2", "l"), (f"Subtotal {a - 30:.2f}", "l"), (f"GST 30.00", "l"), (f"Total {a:.2f}", "l")]


def ctl_hp_laptop_service(rng):  # brand-token trap: 'HP' is a laptop vendor here
    d, a = pick_date(rng), float(rng.choice([2500, 7800, 14999]))
    return [("HP AUTHORISED SERVICE CENTRE", "c"), ("Laptop repair invoice", "c"), (f"Date: {fmt_date(d, 'DD-MM-YYYY')}", "l"), ("Motherboard service", "l"), (f"Total Amount: {a:.2f}", "l")]


def ctl_shell_restaurant(rng):  # brand-token trap
    d, a = pick_date(rng), float(rng.choice([1180, 2290]))
    return [("THE SHELL BEACH RESTAURANT", "c"), (f"Date: {fmt_date(d, 'DD/MM/YYYY')}", "l"), ("Fish thali x2", "l"), (f"Total: {a:.2f}", "l")]


def ctl_hotel(rng):
    d, a = pick_date(rng), float(rng.choice([3500, 5999, 8800]))
    return [("LAKEVIEW RESIDENCY", "c"), ("TAX INVOICE", "c"), (f"Check-in {fmt_date(d, 'DD/MM/YYYY')}", "l"), ("Room Tariff", "l"), (f"Grand Total Rs. {a:,.2f}", "l")]


def ctl_cab(rng):
    d, a = pick_date(rng), float(rng.choice([230, 410, 780]))
    return [("CITY CAB SERVICES", "c"), ("Trip Receipt", "c"), (f"Date: {fmt_date(d, 'DD/MM/YYYY')}", "l"), (f"Fare: Rs {a:.2f}", "l"), (f"Total: Rs {a:.2f}", "l")]


def ctl_pharmacy(rng):
    d, a = pick_date(rng), float(rng.choice([86, 240, 515]))
    return [("BP CHEMISTS & DRUGGISTS", "c"), (f"Date {fmt_date(d, 'DD-MM-YY')}", "l"), ("Paracetamol 650", "l"), (f"Net Amount {a:.2f}", "l")]


def ctl_petrol_adjacent(rng):  # fuel WORDS but not a fuel sale: a lubricant/battery shop
    d, a = pick_date(rng), float(rng.choice([650, 2100, 3400]))
    return [("PETRO MOTOR SPARES", "c"), ("Engine oil 5W30 / battery", "c"), (f"Date: {fmt_date(d, 'DD/MM/YYYY')}", "l"), (f"Total {a:.2f}", "l")]


def ctl_supermarket(rng):
    d, a = pick_date(rng), float(rng.choice([312, 874, 1260]))
    return [("FRESH MART", "c"), (f"{fmt_date(d, 'DD-MON-YYYY')}", "l"), ("Rice 5kg", "l"), ("Oil 1L", "l"), (f"TOTAL {a:.2f}", "l")]


CONTROLS = [("restaurant", ctl_restaurant), ("hp_laptop_service", ctl_hp_laptop_service), ("shell_restaurant", ctl_shell_restaurant), ("hotel", ctl_hotel),
            ("cab", ctl_cab), ("pharmacy_bp", ctl_pharmacy), ("petrol_adjacent_spares", ctl_petrol_adjacent), ("supermarket", ctl_supermarket)]

# ---------------------------------------------------------------------------------------------- rendering / degradation

DEGRADATIONS = ["clean", "faint", "blur", "skew", "noise", "lowres", "rotated90", "cropped_top"]


def render(lines, rng, degradation, font_key="cour"):
    width = rng.choice([384, 480, 576])
    size = {384: 17, 480: 20, 576: 24}[width]
    font_path = FONTS["arial"] if any("\u20b9" in t for t, _ in lines) else FONTS[font_key]
    font = ImageFont.truetype(font_path, size)
    hand = ImageFont.truetype(FONTS["ink"], size + 8)
    lh = int(size * 1.45)
    height = lh * (len(lines) + 4)
    img = Image.new("L", (width, height), 255)
    dr = ImageDraw.Draw(img)
    y = lh
    for text, align in lines:
        f = font
        if text.startswith("@@HAND:"):
            dr.text((width * 0.55, y - 6), "Rs " + text.split(":", 1)[1], fill=60, font=hand)
            y += lh
            continue
        w = dr.textlength(text, font=f)
        x = max(8, (width - w) / 2) if align == "c" else 12
        dr.text((x, y), text, fill=25, font=f)
        y += lh
    if degradation == "faint":
        img = ImageOps.autocontrast(img.point(lambda p: 150 + p * 0.25))
        img = img.point(lambda p: 170 + p * 0.3)
    elif degradation == "blur":
        img = img.filter(ImageFilter.GaussianBlur(1.6))
    elif degradation == "skew":
        img = img.rotate(rng.choice([-6, -4, 4, 6]), expand=True, fillcolor=255)
    elif degradation == "noise":
        px = img.load()
        for _ in range(width * height // 25):
            px[rng.randrange(width), rng.randrange(height)] = rng.choice([0, 40, 200, 255])
    elif degradation == "lowres":
        img = img.resize((int(width * 0.45), int(height * 0.45)), Image.BILINEAR)
    elif degradation == "rotated90":
        img = img.rotate(90, expand=True, fillcolor=255)
    elif degradation == "cropped_top":
        img = img.crop((0, int(lh * 2.4), width, height))
    return img, degradation


def build(out_dir: Path, seed: int):
    (out_dir / "images").mkdir(parents=True, exist_ok=True)
    records = []
    rng_master = random.Random(seed)
    n = 0
    for split, templates in TEMPLATES.items():
        for fam, fn in templates:
            for variant in range(6):
                rng = random.Random(f"{seed}-{fam}-{variant}")
                lines, truth = fn(rng)
                # difficulty: variants 0-2 clean-ish; 3.. degraded, deterministic per (family, variant)
                degradation = ["clean", "clean", rng_master.choice(["faint", "blur"]), rng_master.choice(["skew", "noise"]), rng_master.choice(["lowres", "faint", "blur"]),
                               rng_master.choice(["rotated90", "cropped_top", "skew"])][variant]
                img, deg = render(lines, rng, degradation, font_key=rng.choice(["cour", "consola", "lucon"]))
                n += 1
                rid = f"syn_{split}_{fam}_{variant}"
                if deg == "cropped_top":  # the header is physically gone: brand/dealer cannot be known
                    truth = Truth(truth, pump_name=None, pump_alternates=[], expect_review=True, review_reason="header cropped away: pump name absent")
                img.save(out_dir / "images" / f"{rid}.png")
                records.append({"receipt_id": rid, "source_type": "synthetic", "source_reference": "make_synthetic_fuel.py (invented)", "receipt_family": fam, "split": split,
                                "difficulty": deg, "kind": "fuel", "truth": dict(truth), "annotation_status": "construction_truth"})
    for fam, fn in CONTROLS:
        for variant in range(3):
            rng = random.Random(f"{seed}-ctl-{fam}-{variant}")
            lines = fn(rng)
            degradation = ["clean", rng_master.choice(["faint", "blur"]), rng_master.choice(["skew", "noise"])][variant]
            img, deg = render(lines, rng, degradation, font_key=rng.choice(["cour", "consola", "lucon"]))
            rid = f"ctl_{fam}_{variant}"
            img.save(out_dir / "images" / f"{rid}.png")
            records.append({"receipt_id": rid, "source_type": "generic_control", "source_reference": "make_synthetic_fuel.py (invented non-fuel)", "receipt_family": fam, "split": "control",
                            "difficulty": deg, "kind": "non_fuel", "truth": {"pump_name": None, "date": None, "amount": None, "expect_review": True, "review_reason": "not a fuel receipt"},
                            "annotation_status": "construction_truth"})
    (out_dir / "synthetic_annotations.json").write_text(json.dumps({"seed": seed, "note": "SYNTHETIC - construction labels only; not authentic ground truth.", "records": records}, indent=1), encoding="utf-8")
    print(f"wrote {len(records)} images ({sum(1 for r in records if r['kind']=='fuel')} fuel, {sum(1 for r in records if r['kind']=='non_fuel')} controls) -> {out_dir}")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("out_dir")
    ap.add_argument("--seed", type=int, default=1)
    a = ap.parse_args()
    build(Path(a.out_dir), a.seed)
