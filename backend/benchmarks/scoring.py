"""Scoring for the generalization benchmark: compares what the real pipeline stored for a
synthetic Expense Report against the generator's exact ground truth (see tests/synthetic_reports.py).
Importable by both benchmarks/run_generalization_benchmark.py and the regression tests."""

from __future__ import annotations

from collections import Counter, defaultdict

import synthetic_reports as sr
from app.models.bill import Bill
from app.models.expense_report import Expense
from app.models.validation import ExceptionRecord
from app.services.category_map import normalize_category

REVIEW_STATUSES = {"REVIEW_REQUIRED", "MULTIPLE_CANDIDATES"}


def _same_amount(a, b) -> bool:
    return a is not None and b is not None and abs(float(a) - float(b)) < 0.005


def score_case(case: sr.Case, page_of: dict[str, int], db, report_id: int) -> dict:
    expenses = {e.s_no: e for e in db.query(Expense).filter(Expense.expense_report_id == report_id).all()}
    bills_by_page = {}
    for b in db.query(Bill).filter(Bill.expense_report_id == report_id).all():
        bills_by_page.setdefault(b.page_number, b)
    bill_by_id = {b.id: b for b in bills_by_page.values()}
    exc: dict[int, set[str]] = defaultdict(set)
    for x in db.query(ExceptionRecord).filter(ExceptionRecord.expense_report_id == report_id).all():
        if x.expense_id is not None and x.status != "RESOLVED":
            exc[x.expense_id].add(x.exception_type)
    rid_of_page = {p: rid for rid, p in page_of.items()}
    receipts = {r.rid: r for r in case.receipts}
    out: dict = {"name": case.name, "claims": [], "receipts": [], "row": {}}

    # -------- claim-row extraction
    tp = 0
    field_ok = Counter()
    for c in case.claims:
        e = expenses.get(c.s_no)
        ok_date = e is not None and e.expense_date == c.day
        ok_amt = e is not None and _same_amount(e.claimed_amount, c.amount)
        ok_cat = e is not None and normalize_category(e.category) == normalize_category(c.category)
        field_ok["date"] += ok_date
        field_ok["amount"] += ok_amt
        field_ok["category"] += ok_cat
        tp += bool(ok_date and ok_amt and ok_cat)
    out["row"] = {"truth_rows": len(case.claims), "predicted_rows": len(expenses), "exact_rows": tp, **{f"{k}_ok": v for k, v in field_ok.items()}}

    # -------- receipt (bill) field extraction
    for r in case.receipts:
        b = bills_by_page.get(page_of[r.rid])
        rec = {"rid": r.rid, "template": r.template, "page": page_of[r.rid], "found": b is not None}
        if b is not None:
            rec["amount_ok"] = r.amount is not None and _same_amount(b.amount, r.amount)
            pred_date = b.bill_date
            if not r.date_resolvable:
                rec["date_state"] = "unresolved_correct" if pred_date is None else "guessed_ambiguous"
            else:
                rec["date_state"] = "exact" if pred_date == r.day else ("unresolved" if pred_date is None else "wrong")
            # Strict: the merchant/recipient must have been EXTRACTED as the vendor or description - merely
            # appearing somewhere in the raw text does not count.
            vendor = " ".join(filter(None, [b.vendor, b.description])).lower()
            rec["merchant_ok"] = bool(r.merchant) and r.merchant.lower() in vendor
            rec["txn_ok"] = bool(r.txn_ref) and (getattr(b, "transaction_reference", None) or "") == r.txn_ref
            rec["time_ok"] = bool(r.time) and getattr(b, "transaction_time", None) == r.time
            rec["status_ok"] = bool(r.status) and getattr(b, "payment_status", None) == r.status
            rec["pred_date"] = str(pred_date)
            rec["pred_amount"] = float(b.amount) if b.amount is not None else None
            rec["pred_time"] = getattr(b, "transaction_time", None)
            rec["pred_status"] = getattr(b, "payment_status", None)
            rec["pred_ref"] = getattr(b, "transaction_reference", None)
            rec["date_method"] = getattr(b, "bill_date_method", None)
            rec["notes"] = (b.extraction_notes or "")[:300]
        out["receipts"].append(rec)

    # -------- allocation / verification per claim
    used_bills: Counter = Counter()
    for c in case.claims:
        e = expenses.get(c.s_no)
        if e is not None and e.bill_match_status == "MATCHED" and e.bill_id is not None:
            used_bills[e.bill_id] += 1
    for c in case.claims:
        e = expenses.get(c.s_no)
        rec = {"s_no": c.s_no, "expect": c.expect, "acceptable": sorted(c.acceptable), "found": e is not None}
        if e is None:
            out["claims"].append(rec)
            continue
        matched = e.bill_match_status == "MATCHED" and e.bill_id is not None
        matched_rid = rid_of_page.get(bill_by_id[e.bill_id].page_number) if matched and e.bill_id in bill_by_id else None
        rec.update({
            "match_status": e.bill_match_status, "matched_rid": matched_rid, "validation": e.validation_status,
            "reasons": (e.bill_match_reasons or "")[:400],
            "confidence": e.bill_match_confidence, "exceptions": sorted(exc.get(e.id, [])),
            "bill_shared": bool(matched and used_bills[e.bill_id] > 1 and not (c.is_duplicate_claim or c.shares_bill_with_duplicate)),
        })
        rec["allocation_correct"] = bool(c.expect == "SUPPORTED" and matched and matched_rid in c.acceptable and not rec["bill_shared"])
        rec["fully_verified"] = bool(rec["allocation_correct"] and e.validation_status == "PASS")
        rec["wrong_bill"] = bool(matched and matched_rid not in c.acceptable)
        rec["false_confident"] = bool(rec["wrong_bill"] or (c.expect in ("UNSUPPORTED",) and e.validation_status == "PASS") or (c.expect == "AMBIGUOUS" and matched and rec["wrong_bill"]))
        rec["in_review"] = bool(e.validation_status == "REVIEW_REQUIRED" or e.bill_match_status in REVIEW_STATUSES)
        rec["review_justified"] = bool(rec["in_review"] and c.expect in ("AMBIGUOUS", "UNSUPPORTED"))
        rec["exception_expected"] = c.expect_exception
        rec["exception_found"] = bool(c.expect_exception and (set(c.expect_exception.split("|")) & exc.get(e.id, set())))
        rec["verifiable"] = bool(c.expect == "SUPPORTED" and not c.expect_exception)
        rec["false_duplicate"] = bool("DUPLICATE BILL" in exc.get(e.id, set()) and not c.is_duplicate_claim)
        rec["duplicate_expected"] = c.is_duplicate_claim
        rec["duplicate_not_verified"] = bool(c.is_duplicate_claim and e.validation_status != "PASS")
        out["claims"].append(rec)
    return out


def aggregate(results: list[dict]) -> dict:
    A: Counter = Counter()
    for r in results:
        row = r["row"]
        A["truth_rows"] += row["truth_rows"]; A["pred_rows"] += row["predicted_rows"]; A["exact_rows"] += row["exact_rows"]
        for k in ("date_ok", "amount_ok", "category_ok"):
            A[f"row_{k}"] += row.get(k, 0)
        for rec in r["receipts"]:
            A["receipts"] += 1
            A["receipt_found"] += rec["found"]
            if not rec["found"]:
                continue
            if rec.get("amount_ok") is not None:
                A["amount_ok"] += bool(rec["amount_ok"])
            A["merchant_ok"] += bool(rec["merchant_ok"])
            st = rec["date_state"]
            if st in ("exact", "wrong", "unresolved"):
                A["date_resolvable"] += 1; A[f"date_{st}"] += 1
            else:
                A["date_unresolvable"] += 1; A[f"date_{st}"] += 1
        for c in r["claims"]:
            A["claims"] += 1
            A[f"expect_{c['expect']}"] += 1
            if not c.get("found"):
                continue
            A["alloc_correct"] += c["allocation_correct"]; A["fully_verified"] += c["fully_verified"] and c["verifiable"]
            A["verifiable"] += c["verifiable"]
            A["wrong_bill"] += c["wrong_bill"]; A["false_confident"] += c["false_confident"]
            A["in_review"] += c["in_review"]; A["review_justified"] += c["review_justified"]
            A["false_duplicate"] += c["false_duplicate"]
            if c["exception_expected"]:
                A["exc_expected"] += 1; A["exc_found"] += c["exception_found"]
            if c["duplicate_expected"]:
                A["dup_expected"] += 1; A["dup_caught"] += c["duplicate_not_verified"]
    return dict(A)


def pct(n, d) -> str:
    return f"{n}/{d} = {100 * n / d:.1f}%" if d else "n/a (0)"


def summarize(label: str, A: dict) -> str:
    sup = A.get("expect_SUPPORTED", 0)
    lines = [
        f"== {label} ==",
        f"claim-row extraction  precision {pct(A.get('exact_rows', 0), A.get('pred_rows', 0))} | recall {pct(A.get('exact_rows', 0), A.get('truth_rows', 0))}",
        f"   field accuracy (of {A.get('truth_rows', 0)} truth rows): date {A.get('row_date_ok', 0)}, amount {A.get('row_amount_ok', 0)}, category {A.get('row_category_ok', 0)}",
        f"receipt amount exact  {pct(A.get('amount_ok', 0), A.get('receipt_found', 0))}",
        f"receipt date exact (resolvable)   {pct(A.get('date_exact', 0), A.get('date_resolvable', 0))} | wrong {A.get('date_wrong', 0)} | unresolved {A.get('date_unresolved', 0)}",
        f"ambiguous dates (no evidence)    correctly left unresolved {A.get('date_unresolved_correct', 0)} / {A.get('date_unresolvable', 0)} | guessed {A.get('date_guessed_ambiguous', 0)}",
        f"merchant extraction (token present) {pct(A.get('merchant_ok', 0), A.get('receipt_found', 0))}",
        f"claim->bill allocation correct (SUPPORTED claims) {pct(A.get('alloc_correct', 0), sup)}   fully verified (no exception expected) {pct(A.get('fully_verified', 0), A.get('verifiable', 0))}",
        f"FALSE CONFIDENT matches (of all {A.get('claims', 0)} claims) {pct(A.get('false_confident', 0), A.get('claims', 0))}  [wrong bill {A.get('wrong_bill', 0)}]",
        f"false duplicate flags {A.get('false_duplicate', 0)} | duplicate claims not verified {pct(A.get('dup_caught', 0), A.get('dup_expected', 0))}",
        f"expected exceptions raised {pct(A.get('exc_found', 0), A.get('exc_expected', 0))}",
        f"review-required {pct(A.get('in_review', 0), A.get('claims', 0))} of claims; justified (truth AMBIGUOUS/UNSUPPORTED) {pct(A.get('review_justified', 0), A.get('in_review', 0))}",
    ]
    return "\n".join(lines)
