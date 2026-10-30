"""Synthetic Expense Report generator with exact ground truth, used by the
generalization benchmark (backend/benchmarks/run_generalization_benchmark.py)
and by regression tests.

Everything is authored here, independently of production code: the parser knows
no vendor, amount, date or layout used below. Ground truth is exact by
construction (it is what the generator wrote), so metrics computed against it
are real measurements - but only of what this generator can express. It cannot
measure real handwriting or real photographed receipts (see the benchmark
report's limitations).

Two template FAMILIES are kept apart on purpose:
  * "dev"      - receipt templates / ER layout the implementation was built and
                 tuned against;
  * "heldout"  - different receipt templates, date conventions and a different
                 ER table layout, never used to tune thresholds.
A third family, "heldout2", is generated from fresh seeds and templates and is
used for the final reported numbers once "heldout" has been used for diagnosis.
"""

from __future__ import annotations

import random
from dataclasses import dataclass, field
from datetime import date, timedelta
from io import BytesIO

import fitz

MONTH_ABBR = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]
MONTH_FULL = ["January", "February", "March", "April", "May", "June", "July", "August", "September", "October", "November", "December"]

_ONES = ["", "One", "Two", "Three", "Four", "Five", "Six", "Seven", "Eight", "Nine", "Ten", "Eleven", "Twelve", "Thirteen",
         "Fourteen", "Fifteen", "Sixteen", "Seventeen", "Eighteen", "Nineteen"]
_TENS = ["", "", "Twenty", "Thirty", "Forty", "Fifty", "Sixty", "Seventy", "Eighty", "Ninety"]


def words(n: int) -> str:
    if n == 0:
        return "Zero"
    parts = []
    for div, name in ((10_000_000, "Crore"), (100_000, "Lakh"), (1000, "Thousand"), (100, "Hundred")):
        if n >= div:
            parts.append(f"{words(n // div)} {name}")
            n %= div
    if n:
        parts.append(_ONES[n] if n < 20 else (_TENS[n // 10] + (" " + _ONES[n % 10] if n % 10 else "")))
    return " ".join(parts)


# --------------------------------------------------------------------------- model


@dataclass
class Receipt:
    rid: str
    template: str
    lines: list[str]
    amount: float | None  # amount the receipt really shows (None = unreadable by construction)
    day: date | None  # the true calendar date
    time: str | None = None  # "HH:MM" 24h
    merchant: str | None = None  # a token that must appear in the extracted merchant/recipient
    txn_ref: str | None = None
    status: str | None = None  # COMPLETED | PENDING | FAILED | None
    kind: str = "invoice"  # invoice | ride | payment
    date_raw: str | None = None
    date_resolvable: bool = True  # False: nothing in the report can fix the day/month order
    as_image: bool = False  # render the page as a picture so it must go through OCR
    category_evidence: str | None = None  # e.g. "Flight"; used for category-mismatch truth


@dataclass
class Claim:
    s_no: int
    day: date
    category: str
    details: str
    merchant: str
    amount: float
    # What a correct system may do with this claim:
    #   SUPPORTED   - one of `acceptable` receipts supports it; should be MATCHED to one of them
    #   AMBIGUOUS   - acceptable receipts exist but the evidence legitimately cannot confirm (review is fine,
    #                 a MATCH to a NON-acceptable receipt is not)
    #   UNSUPPORTED - no valid support (missing / pending / failed / reused document): must NOT be verified
    expect: str = "SUPPORTED"
    acceptable: set[str] = field(default_factory=set)
    expect_exception: str | None = None  # e.g. "CATEGORY MISMATCH", "MISSING BILL", "DATE MISMATCH"
    is_duplicate_claim: bool = False  # reuses a document an earlier claim already uses
    shares_bill_with_duplicate: bool = False  # the ORIGINAL claim whose bill a later duplicate claim reuses


@dataclass
class Case:
    name: str
    family: str  # dev | heldout | heldout2
    layout: str  # L1 | L2 | L3
    claims: list[Claim]
    receipts: list[Receipt]
    employee: str = "TEST EMPLOYEE"
    er_id: str = "ER-99000"
    notes: str = ""

    def total(self) -> float:
        return round(sum(c.amount for c in self.claims), 2)


# --------------------------------------------------------------------------- receipt templates
# Each returns a Receipt. `fmt_date` selects how the date is printed.


def _digits(rng: random.Random, n: int) -> str:
    return "".join(str(rng.randint(0, 9)) for _ in range(n))


def _clock(h24: int, minute: int, style: str) -> str:
    if style == "12h":
        suffix = "AM" if h24 < 12 else "PM"
        h = h24 % 12 or 12
        return f"{h}:{minute:02d} {suffix}"
    if style == "12h_lower":
        suffix = "am" if h24 < 12 else "pm"
        h = h24 % 12 or 12
        return f"{h}:{minute:02d} {suffix}"
    return f"{h24:02d}:{minute:02d}"


def date_text(d: date, fmt: str) -> str:
    return {
        "D Mon YYYY": f"{d.day} {MONTH_ABBR[d.month - 1]} {d.year}",
        "D MONTH YYYY": f"{d.day} {MONTH_FULL[d.month - 1].upper()} {d.year}",
        "Mon D, YYYY": f"{MONTH_ABBR[d.month - 1]} {d.day}, {d.year}",
        "DD/MM/YYYY": f"{d.day:02d}/{d.month:02d}/{d.year}",
        "MM/DD/YYYY": f"{d.month:02d}/{d.day:02d}/{d.year}",
        "DD-MM-YYYY": f"{d.day:02d}-{d.month:02d}-{d.year}",
        "ISO": d.isoformat(),
        "M/D/YY": f"{d.month}/{d.day}/{d.year % 100:02d}",
        "D/M/YY": f"{d.day}/{d.month}/{d.year % 100:02d}",
    }[fmt]


def tpl_ride_us(rng, rid, amount, day, hm, merchant="Uber", **kw) -> Receipt:  # dev
    raw = date_text(day, "M/D/YY")
    clock = _clock(hm[0], hm[1], "12h")
    return Receipt(
        rid, "ride_us",
        [merchant, "Thanks for riding, Alex", "We hope you enjoyed your ride this morning.", f"Total ₹{amount:.2f}",
         "Payments", f"Cash. ₹{amount:.2f}", f"{raw} {clock}",
         "This receipt reflects the Suggested Fare and is not a tax invoice.", "Trip details", f"{rng.randint(3, 30)}.{rng.randint(10, 99)} kilometers"],
        amount, day, f"{hm[0]:02d}:{hm[1]:02d}", merchant, None, None, "ride", raw, kw.get("resolvable", True),
        category_evidence="Cab/Taxi",
    )


def tpl_gst_invoice(rng, rid, amount, day, hm, merchant="Orion Travels", fmt="DD/MM/YYYY", **kw) -> Receipt:  # dev
    raw = date_text(day, fmt)
    base = round(amount / 1.05, 2)
    gst = round(amount - base, 2)
    return Receipt(
        rid, "gst_invoice",
        [f"{merchant} Pvt Ltd", f"GSTIN: 29ABCDE{rng.randint(1000, 9999)}F1Z5", "TAX INVOICE", f"Invoice No: INV-{rng.randint(1000, 9999)}",
         f"Invoice Date: {raw}", "Description: Taxi hire charges", f"Subtotal: Rs.{base:,.2f}", f"GST @5%: Rs.{gst:,.2f}", f"Total Amount: Rs.{amount:,.2f}"],
        amount, day, None, merchant, None, None, "invoice", raw, kw.get("resolvable", True), category_evidence="Cab/Taxi",
    )


def tpl_phonepe(rng, rid, amount, day, hm, recipient="Ramesh Kumar", status=None, txn=None, fmt="D Mon YYYY", digits_swap=None, **kw) -> Receipt:  # dev
    txn = txn or _digits(rng, 12)
    raw = date_text(day, fmt)
    shown = f"{int(amount)}" if digits_swap is None else digits_swap  # leading-digit OCR confusion, e.g. "7500"
    lines = ["Amount", f"₹{shown}", f"Rupees {words(int(amount))} Only", "Money Transfer", "To", recipient, f"UPI ID: {_digits(rng, 10)}@axl on PhonePe",
             "From", "Alex Doe", "ICICI Bank - 2910", f"Paid at {_clock(hm[0], hm[1], '12h')}, {raw}", f"UPI Ref No: {txn} Copy", "Payment Details"]
    if status == "PENDING":  # the app prints its status banner ABOVE the "Amount" block
        lines.insert(0, "Payment Pending")
    elif status == "FAILED":
        lines.insert(0, "Payment Failed")
    elif status == "COMPLETED":
        lines.insert(0, "Paid Successfully")
    return Receipt(rid, "phonepe", lines, float(amount), day, f"{hm[0]:02d}:{hm[1]:02d}", recipient, txn, status, "payment", raw, kw.get("resolvable", True))


def tpl_gpay(rng, rid, amount, day, hm, recipient="Sunil Travels", status="COMPLETED", txn=None, fmt="D Mon YYYY", **kw) -> Receipt:  # dev
    txn = txn or _digits(rng, 12)
    raw = date_text(day, fmt)
    word = {"COMPLETED": "Completed", "PENDING": "Pending", "FAILED": "Failed"}[status]
    return Receipt(
        rid, "gpay",
        [recipient, f"₹{int(amount)}", word, f"{raw}, {_clock(hm[0], hm[1], '12h_lower')}", "UPI transaction ID", txn, "Google transaction ID", "CICAgM" + _digits(rng, 10)],
        float(amount), day, f"{hm[0]:02d}:{hm[1]:02d}", recipient, txn, status, "payment", raw, kw.get("resolvable", True),
    )


# held-out templates -------------------------------------------------------------


def tpl_paytm_like(rng, rid, amount, day, hm, recipient="Meera Cabs", status="COMPLETED", txn=None, fmt="DD/MM/YYYY", **kw) -> Receipt:  # heldout
    txn = txn or _digits(rng, 12)
    raw = date_text(day, fmt)
    head = {"COMPLETED": "Payment Successful", "PENDING": "Payment Pending", "FAILED": "Payment Failed"}[status]
    return Receipt(
        rid, "paytm_like",
        [head, f"₹ {amount:,.2f}", "Paid to", recipient, f"{raw} {_clock(hm[0], hm[1], '24h')}", f"Order ID: OD{_digits(rng, 9)}", f"UPI Ref. No: {txn}"],
        float(amount), day, f"{hm[0]:02d}:{hm[1]:02d}", recipient, txn, status, "payment", raw, kw.get("resolvable", True),
    )


def tpl_hotel_invoice(rng, rid, amount, day, hm, merchant="Lakeview Residency", fmt="D MONTH YYYY", **kw) -> Receipt:  # heldout
    raw = date_text(day, fmt)
    out = day + timedelta(days=1)
    return Receipt(
        rid, "hotel_invoice",
        [merchant.upper(), "TAX INVOICE", f"GSTIN : 07AAACL{rng.randint(1000, 9999)}M1Z2", f"Invoice No {rng.randint(10000, 99999)}", f"Check-in {raw}",
         f"Check-out {date_text(out, fmt)}", "Room Tariff", f"Grand Total Rs. {amount:,.2f}", f"Rupees {words(int(amount))} Only"],
        amount, day, None, merchant, None, None, "invoice", raw, kw.get("resolvable", True), category_evidence="Lodging",
    )


def tpl_taxi_slip_labeled(rng, rid, amount, day, hm, merchant="City Cab Services", fmt="MM/DD/YYYY", cue="Date (MM/DD/YYYY)", **kw) -> Receipt:  # heldout
    raw = date_text(day, fmt)
    return Receipt(
        rid, "taxi_slip_labeled",
        [merchant, "Trip Receipt", f"{cue}: {raw}", f"Pickup time: {_clock(hm[0], hm[1], '12h')}", f"Fare: Rs {amount:,.2f}", f"Total: Rs {amount:,.2f}"],
        amount, day, f"{hm[0]:02d}:{hm[1]:02d}", merchant, None, None, "ride", raw, True, category_evidence="Cab/Taxi",
    )


def tpl_fuel_pos(rng, rid, amount, day, hm, merchant="Highway Petro Plaza", fmt="DD-MM-YYYY", **kw) -> Receipt:  # heldout
    raw = date_text(day, fmt)
    return Receipt(
        rid, "fuel_pos",
        [merchant.upper(), f"GSTIN: 33AAAFH{rng.randint(1000, 9999)}K1ZP", "FUEL RECEIPT", f"Date: {raw} Time: {_clock(hm[0], hm[1], '24h')}", "Product: PETROL",
         f"Volume: {amount / 100:.2f} L", f"Amount: Rs.{amount:,.2f}", "Thank you"],
        amount, day, f"{hm[0]:02d}:{hm[1]:02d}", merchant, None, None, "invoice", raw, kw.get("resolvable", True), category_evidence="Fuel",
    )


def tpl_flight_ticket(rng, rid, amount, day, hm, merchant="Skyline Airways", **kw) -> Receipt:  # heldout
    raw = date_text(day, "D Mon YYYY")
    return Receipt(
        rid, "flight_ticket",
        [f"{merchant} e-ticket", "PNR: " + _digits(rng, 6), "Passenger: Alex Doe", f"Flight SK {rng.randint(100, 999)}  {raw}", "Departure 07:45  Arrival 09:05",
         "Base Fare: Rs. 3,333", f"Total Fare: Rs. {amount:,.2f}"],
        amount, day, None, merchant, None, None, "invoice", raw, True, category_evidence="Flight",
    )


# heldout2 templates: written AFTER the implementation was tuned on dev/heldout, never used to tune it ----------


def tpl_ola_like(rng, rid, amount, day, hm, merchant="QuickRide", **kw) -> Receipt:  # heldout2
    raw = f"{MONTH_ABBR[day.month - 1]} {day.day}, {day.year} at {_clock(hm[0], hm[1], '12h')}"
    return Receipt(
        rid, "ola_like",
        [merchant, f"Ride ID CRN{_digits(rng, 10)}", "Booking confirmed", raw, f"Total Bill ₹ {amount:,.2f}", "Paid by Cash", "Thank you for riding"],
        amount, day, f"{hm[0]:02d}:{hm[1]:02d}", merchant, None, None, "ride", raw, True, category_evidence="Cab/Taxi",
    )


def tpl_dotted_ride(rng, rid, amount, day, hm, merchant="MetroGo Cabs", **kw) -> Receipt:  # heldout2 (dotted dates: not supported)
    raw = f"{day.day:02d}.{day.month:02d}.{day.year}"
    return Receipt(
        rid, "dotted_ride",
        [merchant, "Ride Invoice", f"Invoice Date: {raw}", f"Time: {_clock(hm[0], hm[1], '24h')}", f"Fare Rs. {amount:,.2f}", f"Total Rs. {amount:,.2f}"],
        amount, day, f"{hm[0]:02d}:{hm[1]:02d}", merchant, None, None, "ride", raw, True, category_evidence="Cab/Taxi",
    )


def tpl_short_month_year(rng, rid, amount, day, hm, merchant="CityLink Taxi", **kw) -> Receipt:  # heldout2 (DD-Mon-YY: not supported)
    raw = f"{day.day:02d}-{MONTH_ABBR[day.month - 1]}-{day.year % 100:02d}"
    return Receipt(
        rid, "short_month_year",
        [merchant, "Taxi Receipt", f"Date: {raw}", f"Amount Paid: Rs {amount:,.2f}"],
        amount, day, None, merchant, None, None, "ride", raw, True, category_evidence="Cab/Taxi",
    )


def tpl_paytm_ymd(rng, rid, amount, day, hm, recipient="Anil Cab Service", status="COMPLETED", txn=None, **kw) -> Receipt:  # heldout2
    txn = txn or ("T" + _digits(rng, 18))
    raw = f"{day.isoformat()} {hm[0]:02d}:{hm[1]:02d}:{rng.randint(0, 59):02d}"
    head = {"COMPLETED": "Payment Successful", "PENDING": "Payment Pending", "FAILED": "Payment Failed"}[status]
    return Receipt(
        rid, "paytm_ymd",
        [head, f"₹ {amount:,.2f}", f"Paid to {recipient}", raw, f"Txn ID: {txn}"],
        float(amount), day, f"{hm[0]:02d}:{hm[1]:02d}", recipient, txn, status, "payment", raw, True,
    )


def tpl_gpay_alt(rng, rid, amount, day, hm, recipient="Sharma Travels", status="COMPLETED", txn=None, **kw) -> Receipt:  # heldout2
    txn = txn or _digits(rng, 12)
    word = {"COMPLETED": "Payment successful", "PENDING": "Payment pending", "FAILED": "Payment failed"}[status]
    raw = f"{day.day} {MONTH_ABBR[day.month - 1]} {day.year}, {_clock(hm[0], hm[1], '12h')}"
    return Receipt(
        rid, "gpay_alt",
        [f"Paid to {recipient}", f"₹{int(amount)}", word, raw, f"UPI Ref. No. {txn}"],
        float(amount), day, f"{hm[0]:02d}:{hm[1]:02d}", recipient, txn, status, "payment", raw, True,
    )


def tpl_hotel_cue_after(rng, rid, amount, day, hm, merchant="Hillview Lodge", fmt="DD/MM/YYYY", **kw) -> Receipt:  # heldout2
    raw = date_text(day, fmt)
    cue = "(dd/mm/yyyy)" if fmt == "DD/MM/YYYY" else "(mm/dd/yyyy)"
    return Receipt(
        rid, "hotel_cue_after",
        [f"{merchant} - Guest Folio", f"Arrival {raw} {cue}", "Room Tariff", f"Net Payable Rs. {amount:,.2f}"],
        amount, day, None, merchant, None, None, "invoice", raw, True, category_evidence="Lodging",
    )


def tpl_restaurant_words(rng, rid, amount, day, hm, merchant="Spice Garden Restaurant", digits_swap=None, **kw) -> Receipt:  # heldout2
    shown = f"{int(amount)}" if digits_swap is None else digits_swap
    raw = date_text(day, "D Mon YYYY")
    return Receipt(
        rid, "restaurant_words",
        [merchant.upper(), "RESTAURANT BILL", f"Date: {raw}", "Food and beverages", f"Grand Total {shown}", f"Rupees {words(int(amount))} Only"],
        float(amount), day, None, merchant, None, None, "invoice", raw, True, category_evidence="Food",
    )


# heldout3 templates: defined before any heldout3 result was seen; run ONCE, never tuned against ----------------


def tpl_bank_alert(rng, rid, amount, day, hm, recipient="rajesh.cab@okbank", status="COMPLETED", txn=None, **kw) -> Receipt:  # heldout3
    txn = txn or _digits(rng, 12)
    raw = f"{day.day:02d}{MONTH_ABBR[day.month - 1]}{day.year % 100:02d}"  # "14Jun26"
    verb = {"COMPLETED": "debited", "PENDING": "pending", "FAILED": "failed"}[status]
    return Receipt(
        rid, "bank_alert",
        [f"A/c XX{rng.randint(1000, 9999)} {verb} by INR {amount:,.2f}", f"on {raw} {hm[0]:02d}:{hm[1]:02d}", f"to VPA {recipient}", f"UPI Ref No {txn}"],
        float(amount), day, f"{hm[0]:02d}:{hm[1]:02d}", recipient.split("@")[0], txn, status, "payment", raw, True,
    )


def tpl_ride_slash_month(rng, rid, amount, day, hm, merchant="Zippy Cabs", **kw) -> Receipt:  # heldout3
    raw = f"{day.day:02d}/{MONTH_ABBR[day.month - 1]}/{day.year}"  # "14/Jun/2026"
    return Receipt(
        rid, "ride_slash_month",
        [merchant, f"Trip date {raw}", f"Pickup {_clock(hm[0], hm[1], '24h')}", f"Total fare Rs. {amount:,.2f}"],
        amount, day, f"{hm[0]:02d}:{hm[1]:02d}", merchant, None, None, "ride", raw, True, category_evidence="Cab/Taxi",
    )


# heldout4 templates: defined before any heldout4 result was seen; run ONCE, never tuned against ----------------


def tpl_bank_sms(rng, rid, amount, day, hm, merchant="Aman Cabs", status="COMPLETED", txn=None, **kw) -> Receipt:  # heldout4
    txn = txn or _digits(rng, 12)
    raw = f"{day.day:02d}-{day.month:02d}-{day.year % 100:02d}"  # "14-06-26"
    verb = {"COMPLETED": "debited from", "PENDING": "pending on", "FAILED": "declined for"}[status]
    return Receipt(
        rid, "bank_sms",
        [f"Rs.{amount:,.2f} {verb} A/c XX{rng.randint(1000, 9999)} on {raw} {_clock(hm[0], hm[1], '12h')}.", f"Info: UPI/P2M/{txn}/{merchant}", "Avl Bal Rs.18,450.12"],
        float(amount), day, f"{hm[0]:02d}:{hm[1]:02d}", merchant, txn, status, "payment", raw, True,
    )


def tpl_ride_slash_iso(rng, rid, amount, day, hm, merchant="Nova Rides", **kw) -> Receipt:  # heldout4
    raw = f"{day.year}/{day.month:02d}/{day.day:02d}"  # "2026/06/14"
    return Receipt(
        rid, "ride_slash_iso",
        [merchant, "E-Receipt", f"Trip on {raw} {_clock(hm[0], hm[1], '24h')}", f"Amount charged: INR {amount:,.2f}"],
        amount, day, f"{hm[0]:02d}:{hm[1]:02d}", merchant, None, None, "ride", raw, True, category_evidence="Cab/Taxi",
    )


def tpl_train_ticket(rng, rid, amount, day, hm, merchant="Eastern Rail Services", **kw) -> Receipt:  # heldout4
    raw = f"{day.day:02d}-{MONTH_ABBR[day.month - 1]}-{day.year}"
    return Receipt(
        rid, "train_ticket",
        [f"{merchant} - e-Ticket", f"Journey Date: {raw}", f"Train {rng.randint(10000, 99999)} Howrah Express", f"PNR {_digits(rng, 10)}", f"Total Fare Rs. {amount:,.2f}"],
        amount, day, None, merchant, None, None, "invoice", raw, True, category_evidence="Train",
    )


# --------------------------------------------------------------------------- ER rendering


_FONT_FILE = r"C:\Windows\Fonts\arial.ttf"  # carries the rupee sign (U+20B9); the built-in Helvetica draws it as "·"


def _put(page, y: float, line: str, size: float) -> None:
    import os

    if os.path.exists(_FONT_FILE):
        page.insert_text((40, y), line, fontsize=size, fontname="arialx", fontfile=_FONT_FILE)
    else:  # pragma: no cover - non-Windows fallback loses the rupee glyph
        page.insert_text((40, y), line, fontsize=size)


def _wrap_pdf(pages: list[list[str]], image_pages: set[int]) -> bytes:
    doc = fitz.open()
    for index, lines in enumerate(pages):
        if index in image_pages:  # a picture of the text, so the page can only be read through OCR
            scratch = fitz.open()
            sp = scratch.new_page()
            y = 40
            for line in lines:
                _put(sp, y, line, 11)
                y += 16
            png = sp.get_pixmap(dpi=170).tobytes("png")
            rect = sp.rect
            scratch.close()
            page = doc.new_page(width=rect.width, height=rect.height)
            page.insert_image(page.rect, stream=png)
            continue
        page = doc.new_page()
        y = 40
        for line in lines:
            _put(page, y, line, 10)
            y += 14
    buf = BytesIO()
    doc.save(buf)
    doc.close()
    return buf.getvalue()


def _er_header(case: Case) -> list[str]:
    return ["Expense Report", case.er_id, "Amount to be Reimbursed", f"Rs.{case.total():,.2f}", "Report", "July", "Submitted By", case.employee,
            "test@spincotech.com", "Submitted On", "01/08/2026"]


def _rows_l1(case: Case) -> list[str]:
    rows: list[str] = ["EXPENSE SUMMARY", "S.No", "Expense Details", "Category", "Amount (INR)"]
    for c in case.claims:
        rows += [f"{c.s_no}.", c.day.strftime("%d/%m/%Y"), f"Merchant : {c.merchant}", c.category, c.details, f"Rs.{c.amount:,.2f}"]
    return rows


def _rows_l2(case: Case) -> list[list[str]]:
    """Held-out layout: one line per claim (columns separated by spaces), repeated
    column header on every page, rows spread over two pages, a wrapped description."""
    per_page = max(1, (len(case.claims) + 1) // 2)
    out: list[list[str]] = []
    chunks = [case.claims[:per_page], case.claims[per_page:]]
    for index, chunk in enumerate(chunks):
        if not chunk and index:
            continue
        page = ["EXPENSE SUMMARY"] if index == 0 else ["EXPENSE SUMMARY (continued)"]
        page += ["S.No  Date  Category  Description  Amount (INR)"]
        for c in chunk:
            desc = f"{c.details} - {c.merchant}"
            first, _, rest = desc.partition(" - ")
            page.append(f"{c.s_no}  {c.day.strftime('%d/%m/%Y')}  {c.category}  {first}  Rs.{c.amount:,.2f}")
            if rest:
                page.append(f"      {rest}")  # wrapped continuation line
        out.append(page)
    return out


def _rows_l3(case: Case) -> list[list[str]]:
    """Held-out layout 2: label/value columns, date written as '14 Jul 2026', category before date."""
    page = ["EXPENSE SUMMARY", "Sr  Category  Date  Details  Amount"]
    for c in case.claims:
        page.append(f"{c.s_no}) {c.category} | {c.day.day} {MONTH_ABBR[c.day.month - 1]} {c.day.year} | {c.details} ({c.merchant}) | INR {c.amount:,.2f}")
    return [page]


def _rows_l4(case: Case) -> list[list[str]]:
    """Held-out layout 3 (heldout2): every row is one labelled line, em-dash separated."""
    page = ["EXPENSE SUMMARY"]
    for c in case.claims:
        page.append(
            f"{c.s_no} — Date: {c.day.strftime('%d/%m/%Y')} — Category: {c.category} — Details: {c.details} ({c.merchant}) — Amount: Rs. {c.amount:,.2f}"
        )
    return [page]


def _rows_l5(case: Case) -> list[list[str]]:
    """heldout3 layout: columns in a different order with the serial LAST ("... | Rs. 231.00 | #3")."""
    page = ["EXPENSE SUMMARY", "Date | Category | Details | Amount | Ref"]
    for c in case.claims:
        page.append(f"{c.day.strftime('%d/%m/%Y')} | {c.category} | {c.details} ({c.merchant}) | Rs. {c.amount:,.2f} | #{c.s_no}")
    return [page]


def _rows_l6(case: Case) -> list[list[str]]:
    """heldout4 layout: every row spans two lines - '3) 14/07/2026 CAB CHARGES' then the details and amount."""
    page = ["EXPENSE SUMMARY"]
    for c in case.claims:
        page.append(f"{c.s_no}) {c.day.strftime('%d/%m/%Y')} {c.category}")
        page.append(f"     {c.details} ({c.merchant})  Rs. {c.amount:,.2f}")
    return [page]


def _rows_l7(case: Case) -> list[list[str]]:
    """heldout4 layout: one LABEL per line (a form-style block per claim)."""
    page = ["EXPENSE SUMMARY"]
    for c in case.claims:
        page += [f"Sr No: {c.s_no}", f"Date: {c.day.strftime('%d/%m/%Y')}", f"Category: {c.category}", f"Description: {c.details} ({c.merchant})", f"Amount: Rs. {c.amount:,.2f}", ""]
    return [page]


def render_er(case: Case) -> tuple[bytes, dict[str, int]]:
    """Returns (pdf bytes, {receipt rid: 1-based page number})."""
    header = _er_header(case)
    footer = ["REPORT SUMMARY BY CURRENCY", "Total Reimbursable Amount", f"Rs.{case.total():,.2f}", f"{case.employee} ( T001 )"]
    pages: list[list[str]] = []
    if case.layout == "L1":
        pages.append(header + _rows_l1(case) + footer)
    elif case.layout in ("L2", "L3", "L4", "L5", "L6", "L7"):
        table_pages = {"L2": _rows_l2, "L3": _rows_l3, "L4": _rows_l4, "L5": _rows_l5, "L6": _rows_l6, "L7": _rows_l7}[case.layout](case)
        pages.append(header + table_pages[0])
        pages.extend(table_pages[1:])
        pages[-1] = pages[-1] + footer
    else:
        raise ValueError(case.layout)
    page_of: dict[str, int] = {}
    image_pages: set[int] = set()
    filler = ["Supporting documents continue on the next page.", "Reference only."]
    for index, receipt in enumerate(case.receipts):
        if index:
            pages.append(filler)  # keeps adjacent receipts from being grouped as one multi-page bill
        pages.append(receipt.lines)
        page_of[receipt.rid] = len(pages)
        if receipt.as_image:
            image_pages.add(len(pages) - 1)
    return _wrap_pdf(pages, image_pages), page_of
