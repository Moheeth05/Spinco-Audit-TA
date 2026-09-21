"""Content-based document type classification.

Scores real textual evidence for each document type rather than trusting the
filename or the upload channel alone. Filename ER-.../TRIP-... prefixes are
used only as a tie-breaking secondary signal, never as the primary basis.
"""

import re

EXPENSE_REPORT_SIGNALS = [
    re.compile(r"\bExpense Report\b", re.IGNORECASE),
    re.compile(r"\bER-\d+\b"),
    re.compile(r"\bEXPENSE SUMMARY\b", re.IGNORECASE),
    re.compile(r"\bAmount to be Reimbursed\b", re.IGNORECASE),
    re.compile(r"\bReport Summary\b", re.IGNORECASE),
]

TRIP_REPORT_SIGNALS = [
    re.compile(r"\bTRIP-\d+\b"),
    re.compile(r"\bTrip Summary\b", re.IGNORECASE),
    re.compile(r"\bTrip Number\b", re.IGNORECASE),
    re.compile(r"\bDomestic Trip\b", re.IGNORECASE),
    re.compile(r"\bBusiness Purpose\b", re.IGNORECASE),
    re.compile(r"\bApproval History\b", re.IGNORECASE),
]

EXPENSE_REPORT = "EXPENSE_REPORT"
TRIP_REPORT = "TRIP_REPORT"
UNKNOWN = "UNKNOWN"


def classify_text(text: str) -> str:
    if not text or not text.strip():
        return UNKNOWN

    expense_score = sum(1 for pattern in EXPENSE_REPORT_SIGNALS if pattern.search(text))
    trip_score = sum(1 for pattern in TRIP_REPORT_SIGNALS if pattern.search(text))

    if expense_score == 0 and trip_score == 0:
        return UNKNOWN
    if expense_score > trip_score:
        return EXPENSE_REPORT
    if trip_score > expense_score:
        return TRIP_REPORT
    return UNKNOWN


def has_embedded_trip_summary(text: str) -> bool:
    return bool(re.search(r"\bTRIP SUMMARY\b", text, re.IGNORECASE))
