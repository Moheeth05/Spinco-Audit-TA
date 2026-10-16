"""Conservative grouping of consecutive supporting-bill pages into one
logical bill, when real continuation evidence supports it.

Architecture-audit follow-up: bill_pipeline.py previously created exactly
one Bill row per PDF page, so a genuine multi-page invoice (vendor on page
1, total on page 2) became two separate, each-partial records - and a
continuation page with no vendor of its own could pick up a spurious value
via _find_vendor()'s weak fallback (confirmed real case: a continuation
line like "Continued from page 1" read as a vendor name).

Deliberately conservative - adjacency alone is never sufficient (see
group_bill_pages' own docstring). Only two signals ever trigger grouping,
both high-confidence and explicitly named in the real-world requirement:
an explicit continuation marker ("continued", "page 2 of 3") or a repeated
invoice/bill number. A weaker candidate signal - "the group has no total
yet and this page supplies one" - was deliberately NOT used: on its own it
would risk merging two genuinely unrelated adjacent pages (e.g. a cab
receipt missing its total immediately followed by an unrelated personal UPI
screenshot that happens to name an amount), exactly the false merge this
module exists to prevent. A page carrying its own strong evidence of being
a NEW bill (a GSTIN, or a confident vendor-suffix match) is never absorbed
into a preceding group, and a bill-number CONFLICT (both pages have one,
and they differ) is a hard veto regardless of any other signal.
"""
import re
from typing import Any

from app.services.extraction import bill_parser
from app.services.extraction.dates import find_all_dates

_CONTINUATION_MARKER_RE = re.compile(
    r"\b(CONTINUED|CONTD\.?|CONTINUATION)\b|\bPAGE\s+\d+\s+OF\s+\d+\b", re.IGNORECASE
)

# A page listing this many DISTINCT dates is a multi-transaction statement
# (credit card / bank statement), not a single bill - see
# _looks_like_multi_transaction_statement's docstring for the confirmed real
# case this guards against.
_STATEMENT_DATE_THRESHOLD = 5


def _looks_like_multi_transaction_statement(text: str) -> bool:
    """A page listing many distinct dates - each anchoring its own separate
    transaction row - is fundamentally NOT a single bill's continuation, even
    when it prints routine "Page X of Y" pagination. Confirmed real case: a
    10-page ICICI credit card statement, each page explicitly printing
    exactly that pattern while listing a dozen-plus unrelated transactions
    per page - the continuation-marker signal alone would otherwise merge
    unrelated transactions into one meaningless "bill", exactly the false
    merge this module exists to prevent. An absolute veto in both
    directions, checked before any other signal: such a page never starts a
    group that absorbs a follower, and never joins a preceding one either."""
    return len(set(find_all_dates(text))) >= _STATEMENT_DATE_THRESHOLD


def group_bill_pages(pages: list[dict[str, Any]]) -> list[list[dict[str, Any]]]:
    """Partitions bill_pages_from()'s page list (in page order) into logical-
    bill groups. Each returned group is one or more CONSECUTIVE pages -
    grouping only ever extends the immediately preceding group, never reaches
    back across an already-closed one, and never skips a page. A singleton
    group (the overwhelming common case - confirmed true for every page in
    this project's entire real corpus) behaves identically to the pre-
    grouping code: its one page's own text is exactly what
    bill_parser.parse_bill_text() already received before this module
    existed.
    """
    groups: list[list[dict[str, Any]]] = []
    group_has_amount: list[bool] = []
    group_bill_number: list[str | None] = []

    for page in pages:
        text = page["text"]
        if not bill_parser.has_bill_evidence(text) or _looks_like_multi_transaction_statement(text):
            groups.append([page])
            group_has_amount.append(False)
            group_bill_number.append(None)
            continue

        own_parsed = bill_parser.parse_bill_text(text)
        starts_new_bill = bool(own_parsed.gstin) or bill_parser.has_confident_vendor_line(text)

        if groups and not starts_new_bill:
            idx = len(groups) - 1
            conflicting_bill_number = (
                own_parsed.bill_number is not None
                and group_bill_number[idx] is not None
                and own_parsed.bill_number != group_bill_number[idx]
            )
            continuation_signal = not conflicting_bill_number and (
                bool(_CONTINUATION_MARKER_RE.search(text))
                or (own_parsed.bill_number is not None and own_parsed.bill_number == group_bill_number[idx])
            )
            if continuation_signal:
                groups[idx].append(page)
                if own_parsed.amount is not None:
                    group_has_amount[idx] = True
                if group_bill_number[idx] is None and own_parsed.bill_number is not None:
                    group_bill_number[idx] = own_parsed.bill_number
                continue

        groups.append([page])
        group_has_amount.append(own_parsed.amount is not None)
        group_bill_number.append(own_parsed.bill_number)

    return groups
