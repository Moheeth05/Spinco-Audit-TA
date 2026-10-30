"""Scenario library for the generalization benchmark. See synthetic_reports.py.

A scenario is a labelled situation (same-amount claims, duplicate screenshots,
pending payments, ambiguous dates, category conflicts, ...). It is instantiated
once per template FAMILY so the same situation is measured on receipt
templates, date formats and ER layouts the implementation was and was not
developed against.
"""

from __future__ import annotations

import random
from datetime import date
from functools import partial

import synthetic_reports as sr
from synthetic_reports import Case, Claim

YEAR = 2026

# ---- template pools per family: (payment_template, ride_template, invoice_template) ----
FAMILIES: dict[str, dict] = {
    "dev": {
        "layouts": ["L1"],
        "payment": [sr.tpl_phonepe, sr.tpl_gpay],
        "ride": [sr.tpl_ride_us, sr.tpl_gst_invoice],
        "hotel": [sr.tpl_gst_invoice],
        "payment_fmt": ["D Mon YYYY", "D Mon YYYY"],
    },
    "heldout": {
        "layouts": ["L2", "L3"],
        "payment": [sr.tpl_paytm_like, partial(sr.tpl_phonepe, fmt="D MONTH YYYY"), partial(sr.tpl_gpay, fmt="ISO")],
        "ride": [sr.tpl_taxi_slip_labeled],
        "hotel": [sr.tpl_hotel_invoice],
        "payment_fmt": ["DD/MM/YYYY", "D MONTH YYYY", "ISO"],
    },
    # heldout2: written after the implementation was tuned on dev/heldout - receipt templates, date
    # formats and an ER layout that were never used to tune anything. Includes formats the parser is
    # known NOT to read (dotted dates, DD-Mon-YY) so the report states real limits.
    "heldout2": {
        "layouts": ["L2", "L4"],
        "payment": [sr.tpl_paytm_ymd, sr.tpl_gpay_alt],
        "ride": [sr.tpl_ola_like, sr.tpl_dotted_ride, sr.tpl_short_month_year],
        "hotel": [sr.tpl_hotel_cue_after],
        "cued": (sr.tpl_hotel_cue_after, "LODGING"),
        "leading": (sr.tpl_restaurant_words, "MARKETING EXPENSES - FOOD"),
        "payment_fmt": [],
    },
    # heldout3: defined before any of its results were seen; run once, never tuned against.
    "heldout3": {
        "layouts": ["L5", "L4"],
        "payment": [sr.tpl_bank_alert, sr.tpl_gpay_alt],
        "ride": [sr.tpl_ride_slash_month, sr.tpl_ola_like],
        "hotel": [sr.tpl_hotel_cue_after],
        "cued": (sr.tpl_hotel_cue_after, "LODGING"),
        "leading": (sr.tpl_restaurant_words, "MARKETING EXPENSES - FOOD"),
        "payment_fmt": [],
    },
    # heldout4: defined before any of its results were seen; run once, never tuned against.
    "heldout4": {
        "layouts": ["L6", "L7"],
        "payment": [sr.tpl_bank_sms, sr.tpl_paytm_ymd],
        "ride": [sr.tpl_ride_slash_iso, sr.tpl_ola_like],
        "hotel": [sr.tpl_hotel_cue_after],
        "cued": (sr.tpl_hotel_cue_after, "LODGING"),
        "leading": (sr.tpl_restaurant_words, "MARKETING EXPENSES - FOOD"),
        "payment_fmt": [],
    },
}


def _d(month: int, day: int) -> date:
    return date(YEAR, month, day)


class _Ctx:
    def __init__(self, family: str, seed: int):
        self.family = family
        self.cfg = FAMILIES[family]
        self.rng = random.Random(f"{family}-{seed}")
        self.layout = self.cfg["layouts"][seed % len(self.cfg["layouts"])]
        self.n = 0

    def rid(self) -> str:
        self.n += 1
        return f"r{self.n}"

    def payment(self, **kw):
        return self.rng.choice(self.cfg["payment"])

    def ride(self):
        return self.rng.choice(self.cfg["ride"])

    def time(self):
        return self.rng.randint(8, 21), self.rng.randint(0, 59)


def _case(ctx: _Ctx, name: str, claims: list[Claim], receipts: list[sr.Receipt], notes: str = "") -> Case:
    return Case(name=f"{ctx.family}:{name}", family=ctx.family, layout=ctx.layout, claims=claims, receipts=receipts,
                er_id=f"ER-{ctx.rng.randint(50000, 59999)}", notes=notes)


def _claim(s_no, day, category, amount, receipts: list[sr.Receipt], expect="SUPPORTED", details="Local travel", merchant="Vendor", **kw) -> Claim:
    return Claim(s_no, day, category, details, merchant, amount, expect, {r.rid for r in receipts}, **kw)


# ----------------------------------------------------------------------------- scenarios


def same_amount_diff_dates(ctx: _Ctx) -> Case:
    amount = float(ctx.rng.choice([150, 230, 310]))
    days = [_d(7, 3), _d(7, 15), _d(7, 22)]
    tpl = ctx.ride()
    rec = [tpl(ctx.rng, ctx.rid(), amount, d, ctx.time()) for d in days]
    claims = [_claim(i + 1, d, "CAB CHARGES", amount, [r]) for i, (d, r) in enumerate(zip(days, rec))]
    ctx.rng.shuffle(rec)
    return _case(ctx, "same_amount_diff_dates", claims, rec, "one receipt prints a day>12 which proves the format for the ambiguous ones")


def same_amount_diff_dates_unproven(ctx: _Ctx) -> Case:
    amount = float(ctx.rng.choice([150, 230, 310]))
    days = [_d(7, 3), _d(7, 5), _d(7, 9)]  # every date ambiguous, nothing proves the order
    tpl = ctx.ride()
    rec = [tpl(ctx.rng, ctx.rid(), amount, d, ctx.time(), resolvable=False) for d in days]
    claims = [_claim(i + 1, d, "CAB CHARGES", amount, [r], expect="AMBIGUOUS") for i, (d, r) in enumerate(zip(days, rec))]
    for c in claims:
        c.acceptable = {r.rid for r in rec if r.day == c.day}
    return _case(ctx, "same_amount_diff_dates_unproven", claims, rec)


def same_day_distinct_times(ctx: _Ctx) -> Case:
    amount = float(ctx.rng.choice([67, 120, 240]))
    day = _d(7, 24)
    tpl = ctx.payment()
    r1 = tpl(ctx.rng, ctx.rid(), amount, day, (12, 22), status="COMPLETED", recipient="Vikram Auto")
    r2 = tpl(ctx.rng, ctx.rid(), amount, day, (18, 40), status="COMPLETED", recipient="Vikram Auto")
    both = [r1, r2]
    claims = [_claim(1, day, "CAB CHARGES", amount, both), _claim(2, day, "CAB CHARGES", amount, both)]
    return _case(ctx, "same_day_distinct_times", claims, both, "two separate completed payments, same amount/day, different times")


def duplicate_screenshot_reused(ctx: _Ctx) -> Case:
    amount = float(ctx.rng.choice([67, 120, 240]))
    day = _d(7, 24)
    tpl = ctx.payment()
    r1 = tpl(ctx.rng, ctx.rid(), amount, day, (12, 22), status="COMPLETED", recipient="Vikram Auto", txn="552211003344")
    r2 = tpl(ctx.rng, ctx.rid(), amount, day, (12, 22), status="COMPLETED", recipient="Vikram Auto", txn="552211003344")
    r2.lines = list(r1.lines)  # the very same screenshot pasted twice
    # The second claim may legitimately point at the same (collapsed) screenshot - what must happen is
    # that it is flagged as a duplicate and NOT verified, while the first claim stays verified.
    claims = [_claim(1, day, "CAB CHARGES", amount, [r1, r2], shares_bill_with_duplicate=True),
              _claim(2, day, "CAB CHARGES", amount, [r1, r2], expect="UNSUPPORTED", is_duplicate_claim=True, expect_exception="DUPLICATE BILL")]
    return _case(ctx, "duplicate_screenshot_reused", claims, [r1, r2], "one payment, shown twice, claimed twice")


def same_time_different_txn(ctx: _Ctx) -> Case:
    amount = float(ctx.rng.choice([67, 120, 240]))
    day = _d(7, 24)
    tpl = ctx.payment()
    r1 = tpl(ctx.rng, ctx.rid(), amount, day, (12, 22), status="COMPLETED", recipient="Vikram Auto", txn="100000000001")
    r2 = tpl(ctx.rng, ctx.rid(), amount, day, (12, 22), status="COMPLETED", recipient="Vikram Auto", txn="100000000002")
    both = [r1, r2]
    claims = [_claim(1, day, "CAB CHARGES", amount, both), _claim(2, day, "CAB CHARGES", amount, both)]
    return _case(ctx, "same_time_different_txn", claims, both, "different transaction ids prove two payments despite identical time")


def pending_failed(ctx: _Ctx) -> Case:
    tpl = ctx.payment()
    ok = tpl(ctx.rng, ctx.rid(), 180.0, _d(7, 14), (10, 5), status="COMPLETED", recipient="Prakash Cabs")
    pend = tpl(ctx.rng, ctx.rid(), 260.0, _d(7, 16), (11, 15), status="PENDING", recipient="Prakash Cabs")
    fail = tpl(ctx.rng, ctx.rid(), 340.0, _d(7, 17), (9, 50), status="FAILED", recipient="Prakash Cabs")
    claims = [_claim(1, _d(7, 14), "CAB CHARGES", 180.0, [ok]),
              _claim(2, _d(7, 16), "CAB CHARGES", 260.0, [], expect="UNSUPPORTED"),
              _claim(3, _d(7, 17), "CAB CHARGES", 340.0, [], expect="UNSUPPORTED")]
    return _case(ctx, "pending_failed", claims, [ok, pend, fail])


def missing_bill(ctx: _Ctx) -> Case:
    tpl = ctx.ride()
    r = tpl(ctx.rng, ctx.rid(), 220.0, _d(7, 20), ctx.time())
    claims = [_claim(1, _d(7, 20), "CAB CHARGES", 220.0, [r]),
              _claim(2, _d(7, 21), "CAB CHARGES", 410.0, [], expect="UNSUPPORTED", expect_exception="MISSING BILL")]
    return _case(ctx, "missing_bill", claims, [r])


def ambiguous_date_no_evidence(ctx: _Ctx) -> Case:
    tpl = ctx.payment()
    day = _d(7, 2)  # 07/02/2026: 2 July or 7 February - nothing in the document says which
    r = tpl(ctx.rng, ctx.rid(), 275.0, day, (14, 5), status="COMPLETED", recipient="Neha Taxi", fmt="DD/MM/YYYY", resolvable=False)
    claims = [_claim(1, day, "CAB CHARGES", 275.0, [r], expect="AMBIGUOUS")]
    return _case(ctx, "ambiguous_date_no_evidence", claims, [r], "numeric date with no cue anywhere: must stay unresolved, never guessed")


def labeled_dmy_february_receipt(ctx: _Ctx) -> Case:
    """A cue states DD/MM/YYYY, so 07/02/2026 is 7 February. The claim (2 July) must NOT be verified by it."""
    tpl, category = ctx.cfg.get("cued", (sr.tpl_taxi_slip_labeled, "CAB CHARGES"))
    r = tpl(ctx.rng, ctx.rid(), 190.0, date(YEAR, 2, 7), (10, 0), fmt="DD/MM/YYYY", cue="Date (DD/MM/YYYY)")
    claims = [_claim(1, _d(7, 2), category, 190.0, [], expect="UNSUPPORTED", expect_exception="DATE MISMATCH")]
    claims[0].acceptable = {r.rid}
    return _case(ctx, "labeled_dmy_february_receipt", claims, [r], "a genuine February receipt in a July claim list")


def labeled_mdy_july_receipt(ctx: _Ctx) -> Case:
    tpl, category = ctx.cfg.get("cued", (sr.tpl_taxi_slip_labeled, "CAB CHARGES"))
    r = tpl(ctx.rng, ctx.rid(), 190.0, _d(7, 2), (10, 0), fmt="MM/DD/YYYY", cue="Date (MM/DD/YYYY)")
    claims = [_claim(1, _d(7, 2), category, 190.0, [r])]
    return _case(ctx, "labeled_mdy_july_receipt", claims, [r], "the same string 07/02/2026 means 2 July when the cue says MM/DD/YYYY")


def mixed_formats_in_one_pdf(ctx: _Ctx) -> Case:
    a = sr.tpl_ride_us(ctx.rng, ctx.rid(), 210.0, _d(7, 4), (9, 10))
    a_proof = sr.tpl_ride_us(ctx.rng, ctx.rid(), 95.0, _d(7, 21), (19, 40))
    # 06/07/2026 is ambiguous on its own, but a same-template invoice in the same report prints
    # 18/07/2026 (day > 12), which proves DD/MM for that template - so it IS resolvable.
    b = sr.tpl_gst_invoice(ctx.rng, ctx.rid(), 840.0, _d(7, 6), (0, 0), fmt="DD/MM/YYYY")
    b_proof = sr.tpl_gst_invoice(ctx.rng, ctx.rid(), 1260.0, _d(7, 18), (0, 0), fmt="DD/MM/YYYY")
    claims = [_claim(1, _d(7, 4), "CAB CHARGES", 210.0, [a]), _claim(2, _d(7, 21), "CAB CHARGES", 95.0, [a_proof]),
              _claim(3, _d(7, 6), "CAB CHARGES", 840.0, [b]), _claim(4, _d(7, 18), "CAB CHARGES", 1260.0, [b_proof])]
    return _case(ctx, "mixed_formats_in_one_pdf", claims, [a, a_proof, b, b_proof], "US-style rides and DD/MM invoices must not be forced to one convention")


def category_conflict_payment(ctx: _Ctx) -> Case:
    tpl = ctx.payment()
    r = tpl(ctx.rng, ctx.rid(), 480.0, _d(7, 11), (13, 0), status="COMPLETED", recipient="Rapid Cab Service")
    claims = [_claim(1, _d(7, 11), "MARKETING EXPENSES - FOOD", 480.0, [r], expect_exception="CATEGORY MISMATCH")]
    return _case(ctx, "category_conflict_payment", claims, [r], "amount and date agree but the recipient is a cab service")


def flight_claimed_as_lodging(ctx: _Ctx) -> Case:
    r = sr.tpl_flight_ticket(ctx.rng, ctx.rid(), 4797.0, _d(7, 19), (7, 45))
    claims = [_claim(1, _d(7, 19), "LODGING", 4797.0, [r], expect_exception="CATEGORY MISMATCH", details="Air ticket")]
    return _case(ctx, "flight_claimed_as_lodging", claims, [r])


def flight_claimed_as_airfare(ctx: _Ctx) -> Case:
    r = sr.tpl_flight_ticket(ctx.rng, ctx.rid(), 4797.0, _d(7, 19), (7, 45))
    claims = [_claim(1, _d(7, 19), "AIRFARE", 4797.0, [r], details="Air ticket")]
    return _case(ctx, "flight_claimed_as_airfare", claims, [r])


def leading_digit_ocr_confusion(ctx: _Ctx) -> Case:
    tpl, category = ctx.cfg.get("leading", (sr.tpl_phonepe, "CAB CHARGES"))
    swap = "7500" if tpl is sr.tpl_phonepe else "4500"  # the rupee glyph read as a leading digit
    r1 = tpl(ctx.rng, ctx.rid(), 500, _d(7, 19), (15, 9), recipient="Sara Urow", digits_swap=swap)
    r2 = tpl(ctx.rng, ctx.rid(), 500, _d(7, 20), (19, 11), recipient="Sara Urow", digits_swap=swap)
    claims = [_claim(1, _d(7, 19), category, 500.0, [r1]), _claim(2, _d(7, 20), category, 500.0, [r2])]
    return _case(ctx, "leading_digit_ocr_confusion", claims, [r1, r2], "the rupee glyph read as a digit: written amount says Five Hundred")


def decoys_same_amount_other_dates(ctx: _Ctx) -> Case:
    tpl = ctx.payment()
    amount = 135.0
    good1 = tpl(ctx.rng, ctx.rid(), amount, _d(7, 8), (9, 0), status="COMPLETED", recipient="Gopal Cabs")
    decoy1 = tpl(ctx.rng, ctx.rid(), amount, _d(7, 23), (9, 5), status="COMPLETED", recipient="Gopal Cabs")
    good2 = tpl(ctx.rng, ctx.rid(), amount, _d(7, 27), (20, 1), status="COMPLETED", recipient="Gopal Cabs")
    claims = [_claim(1, _d(7, 8), "CAB CHARGES", amount, [good1]), _claim(2, _d(7, 27), "CAB CHARGES", amount, [good2]),
              # same-amount receipts exist on other days: either "no bill" or "the nearest one has the wrong date"
              # is a correct refusal; what must not happen is adopting one of them silently.
              _claim(3, _d(7, 29), "CAB CHARGES", amount, [], expect="UNSUPPORTED", expect_exception="MISSING BILL|DATE MISMATCH|BILL MATCH REVIEW REQUIRED")]
    return _case(ctx, "decoys_same_amount_other_dates", claims, [good1, decoy1, good2], "a same-amount receipt on another day must not be adopted")


def ocr_only_pages(ctx: _Ctx) -> Case:
    tpl = ctx.payment()
    r1 = tpl(ctx.rng, ctx.rid(), 245.0, _d(7, 14), (11, 30), status="COMPLETED", recipient="Dinesh Taxi")
    r2 = tpl(ctx.rng, ctx.rid(), 390.0, _d(7, 16), (17, 45), status="COMPLETED", recipient="Dinesh Taxi")
    for r in (r1, r2):
        r.as_image = True
    claims = [_claim(1, _d(7, 14), "CAB CHARGES", 245.0, [r1]), _claim(2, _d(7, 16), "CAB CHARGES", 390.0, [r2])]
    return _case(ctx, "ocr_only_pages", claims, [r1, r2], "pages exist only as pictures; local Tesseract OCR must read them")


def fuel_receipt_for_fuel_claim(ctx: _Ctx) -> Case:
    r = sr.tpl_fuel_pos(ctx.rng, ctx.rid(), 1800.0, _d(7, 15), (8, 30))
    claims = [_claim(1, _d(7, 15), "OWN CAR PETROL", 1800.0, [r], details="Petrol for site visit")]
    return _case(ctx, "fuel_receipt_for_fuel_claim", claims, [r])


def train_ticket_for_train_claim(ctx: _Ctx) -> Case:
    r = sr.tpl_train_ticket(ctx.rng, ctx.rid(), 1245.0, _d(7, 16), (6, 10))
    claims = [_claim(1, _d(7, 16), "TRAIN / BUS FARE", 1245.0, [r], details="Train to site")]
    return _case(ctx, "train_ticket_for_train_claim", claims, [r])


def split_payment(ctx: _Ctx) -> Case:
    tpl = ctx.payment()
    r1 = tpl(ctx.rng, ctx.rid(), 600.0, _d(7, 12), (10, 0), status="COMPLETED", recipient="Ravi Travels")
    r2 = tpl(ctx.rng, ctx.rid(), 400.0, _d(7, 12), (10, 30), status="COMPLETED", recipient="Ravi Travels")
    claims = [_claim(1, _d(7, 12), "CAB CHARGES", 1000.0, [r1, r2], expect="AMBIGUOUS")]
    return _case(ctx, "split_payment", claims, [r1, r2], "one claim paid in two instalments: needs a human, must not match either alone")


SCENARIOS = [
    same_amount_diff_dates, same_amount_diff_dates_unproven, same_day_distinct_times, duplicate_screenshot_reused, same_time_different_txn,
    pending_failed, missing_bill, ambiguous_date_no_evidence, labeled_dmy_february_receipt, labeled_mdy_july_receipt,
    mixed_formats_in_one_pdf, category_conflict_payment, flight_claimed_as_lodging, flight_claimed_as_airfare,
    leading_digit_ocr_confusion, decoys_same_amount_other_dates, ocr_only_pages, split_payment, fuel_receipt_for_fuel_claim, train_ticket_for_train_claim,
]


def build_cases(family: str, seed: int = 1) -> list[Case]:
    cases = []
    for index, scenario in enumerate(SCENARIOS):
        ctx = _Ctx(family, seed * 100 + index)
        cases.append(scenario(ctx))
    return cases
