"""Evidence read from UPI / wallet transaction screenshots (Google Pay, PhonePe,
Paytm, BHIM, bank apps...): status, transaction time and reference.

A payment screenshot is proof of a TRANSACTION, not a merchant-issued invoice,
and a transaction is only proof of payment when it COMPLETED. Nothing here
decides a match or a category; it only reads what the screenshot prints:

  * status  - COMPLETED / PENDING / FAILED, or None when the page prints none.
              Failed outranks pending outranks completed (a page that says
              "Payment failed" is failed even if "success" appears elsewhere).
  * time    - "HH:MM" (24 h), only when a clock time is actually readable; AM/PM
              is honoured. A time that cannot be read stays None - it is never
              defaulted, so "same time" can never be concluded from absence.
  * reference - the UPI reference / transaction id when one is printed.

Never invents a value; every field is None when not present.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

_APP = re.compile(r"\b(?:UPI|BHIM|PHONEPE|PHONE\s*PE|GOOGLE\s*PAY|GPAY|G\s*PAY|PAYTM|AMAZON\s*PAY|MOBIKWIK|FREECHARGE|CRED)\b", re.IGNORECASE)
_PAYMENT_WORD = re.compile(r"\b(?:paid|payment|transaction|transfer|txn|sent)\b", re.IGNORECASE)

_FAILED = re.compile(r"\b(?:failed|failure|unsuccessful|declined|rejected|cancell?ed|not\s+completed|reversed)\b", re.IGNORECASE)
_PENDING = re.compile(r"\b(?:pending|processing|in[\s-]*progress|awaiting|initiated|under\s+process)\b", re.IGNORECASE)
_COMPLETED = re.compile(r"\b(?:completed|successful(?:ly)?|success|paid\s+successfully)\b", re.IGNORECASE)

_CLOCK_12H = re.compile(r"\b(\d{1,2}):(\d{2})(?::\d{2})?\s*([AaPp])\.?[Mm]\.?(?![A-Za-z])")
_CLOCK_24H = re.compile(r"(?<![\d:])([01]?\d|2[0-3]):([0-5]\d)(?::[0-5]\d)?(?![\d:])(?!\s*[AaPp]\.?[Mm])")
_DATE_HINT = re.compile(r"\d{1,2}[/-]\d{1,2}[/-]\d{2,4}|\d{4}-\d{1,2}-\d{1,2}|\b(?:jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\b", re.IGNORECASE)

_REFERENCES = [
    re.compile(r"UPI\s*Ref(?:erence)?\.?\s*(?:No\.?|Number|ID)?\s*[:#-]?\s*(\d{9,16})", re.IGNORECASE),
    re.compile(r"UPI\s*(?:transaction|txn)\s*(?:ID|No\.?)\s*[:#-]?\s*(\d{9,16})", re.IGNORECASE),
    re.compile(r"(?:Transaction|Txn)\s*(?:ID|No\.?|Ref(?:erence)?(?:\s*No\.?)?)\s*[:#-]?\s*([A-Z0-9]{8,30})\b", re.IGNORECASE),
]


@dataclass(frozen=True)
class PaymentEvidence:
    is_payment: bool = False
    status: str | None = None  # COMPLETED | PENDING | FAILED
    time: str | None = None  # HH:MM, 24 h
    time_raw: str | None = None
    reference: str | None = None
    recipient: str | None = None  # who was paid, only when the page says so explicitly ("Paid to X" / a "To" line)


def _to_24h(hour: int, minute: int, meridiem: str | None) -> str | None:
    if minute > 59:
        return None
    if meridiem:
        if not 1 <= hour <= 12:
            return None
        hour = hour % 12 + (12 if meridiem.lower() == "p" else 0)
    elif hour > 23:
        return None
    return f"{hour:02d}:{minute:02d}"


def _find_status(lines: list[str]) -> str | None:
    found = set()
    for line in lines:
        text = line.strip()
        if not text or len(text) > 60:  # status banners are short; long prose is boilerplate/disclaimer
            continue
        if _FAILED.search(text):
            found.add("FAILED")
        elif _PENDING.search(text):
            found.add("PENDING")
        elif _COMPLETED.search(text):
            found.add("COMPLETED")
    for status in ("FAILED", "PENDING", "COMPLETED"):
        if status in found:
            return status
    return None


def _find_time(lines: list[str]) -> tuple[str | None, str | None]:
    """Prefer a clock time on a line that also carries a date (the transaction
    timestamp), else the first clock time on the page."""
    ordered = [l for l in lines if _DATE_HINT.search(l)] + [l for l in lines if not _DATE_HINT.search(l)]
    for line in ordered:
        m = _CLOCK_12H.search(line)
        if m:
            value = _to_24h(int(m.group(1)), int(m.group(2)), m.group(3))
            if value:
                return value, m.group(0)
        m = _CLOCK_24H.search(line)
        if m:
            value = _to_24h(int(m.group(1)), int(m.group(2)), None)
            if value:
                return value, m.group(0)
    return None, None


def _find_reference(text: str) -> str | None:
    flat = re.sub(r"[ \t]*\n[ \t]*", " ", text)  # a value printed on the line below its label
    for pattern in _REFERENCES:
        m = pattern.search(flat)
        if m:
            return m.group(1).upper()
    return None


_PAID_TO = re.compile(r"\b(?:paid|payment|sent|transferred|money\s+sent|paying)\s+to\s*[:\-]?\s*([^\n]{2,60})", re.IGNORECASE)
_RECIPIENT_NOISE = re.compile(r"\s*(?:\bvia\b.*|\bon\b\s+(?:upi|phonepe|google).*|[|\u2022].*)$", re.IGNORECASE)


def _find_recipient(lines: list[str]) -> str | None:
    """The payee, taken only from an explicit 'Paid to X' / 'Payment to X' phrase or from the line right
    after a bare 'To' label - never inferred from an arbitrary name on the page."""
    for index, raw in enumerate(lines):
        line = raw.strip()
        m = _PAID_TO.search(line)
        if m and len(line) <= 90:
            name = _RECIPIENT_NOISE.sub("", m.group(1)).strip(" :-")
            if name and not name.isdigit():
                return name
        if line.lower() in ("to", "to:", "paid to", "paid to:"):
            nxt = next((l.strip() for l in lines[index + 1 :] if l.strip()), "")
            if nxt and len(nxt) <= 60 and not nxt.lower().startswith(("upi id", "from")):
                return _RECIPIENT_NOISE.sub("", nxt).strip(" :-") or None
    return None


def extract(text: str | None) -> PaymentEvidence:
    text = text or ""
    lines = text.splitlines()
    reference = _find_reference(text)
    is_payment = bool(reference) or (bool(_APP.search(text)) and bool(_PAYMENT_WORD.search(text)))
    if not is_payment:
        return PaymentEvidence()
    time, time_raw = _find_time(lines)
    return PaymentEvidence(True, _find_status(lines), time, time_raw, reference, _find_recipient(lines))
