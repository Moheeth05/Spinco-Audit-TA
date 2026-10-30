# Receipt/Supporting-Bill Extraction: Diagnostic Baseline (2026-10-13)

**Diagnostic only — no extraction logic or business rules changed.** Full data: `receipt-extraction-baseline-20261013.json`, `page-census.json`, `bill-logic-parity.json`, `ocr-sample-evaluation.json`.

## Corpus census

168 pages across all 6 reference PDFs. 152 are "bill-region" pages (after the Expense Summary table), 143 have detectable bill evidence, **112 need OCR** (no usable native text). One report (`ER-40782.pdf`) needed zero OCR — all 9 supporting pages are native-text app screenshots/digital invoices, not photos. `vivek-bhai-report-ER41220.pdf` is the opposite extreme — all 54 of its bill pages needed OCR.

A bug was caught and fixed while building this census: the boundary between "Expense Report content" and "bill region" must come from `find_report_summary_page()` (what the real production code uses), not `content_pages()`'s own last page — the latter over-extended to all 10 pages for `ER-40782.pdf`, hiding its entire bill region. Corrected before any evaluation ran.

## Business-logic parity: still 100%

Node's `billParser.mjs` vs Python's `bill_parser.py`, fed identical already-extracted text, across all **143** bill-evidence pages: **vendor, billNumber, billDate, amount, gstin, categoryGuess — 143/143 (100%) every field.** No regression; this extends the earlier 100/100 unit-level parity to the full real corpus.

**This is not the bottleneck.** The sample evaluation below shows real-world accuracy is much lower — both pipelines share the same rules, so 100% rule-parity doesn't mean 100% real accuracy.

## Real-world accuracy: 13 human-verified samples

Hand-picked to cover the hard cases: clean thermal receipts, a handwritten voucher, a handwritten bilingual cash memo, a digital invoice, payment screenshots, a hotel invoice vs. its separate card-swipe slip, and a multi-transaction bank statement.

**Amount accuracy: Python 3/13 (23%), Node 5/13 (38%).** Most failures are the *safe* kind — returning `null` rather than a wrong value (Python: 8 missing, 2 wrong; Node: 6 missing, 2 wrong) — but 2 each are genuinely wrong, confident values.

Two samples' ground truth was corrected mid-evaluation after cross-checking the extracted GSTIN/vendor text against my own claimed page numbers — a transcription error on my part (counting pages in a 26-page PDF), not a pipeline bug. Documented in full in the JSON report, including exactly how it changed the scoring.

### The one finding that matters most

On the 2 samples where the correct answer is "no single bill total exists" (a credit-card statement, a FASTag toll statement — multi-transaction documents, not bills), **Python confidently extracted a wrong number both times** (once the card's ₹1,13,824.75 *balance*, exactly what the task warned against). **Node correctly returned null both times — but not because its logic is better.** Business-logic parity confirms the rules are identical; Node's OCR just happened to be noisy enough on these two pages that no clean label match surfaced. The underlying gap — nothing in `bill_parser.py`/`billParser.mjs` guards against a multi-transaction statement being read as "the bill" — exists in both pipelines and is unfixed. A near-identical guard already exists for a different concern (bill *grouping*); it was never extended to basic amount extraction.

### Also confirmed, lower priority

- **Vendor extraction picks up adjacent labels, not the vendor** on 5 of 13 samples — "Paid to", "Name" (form labels), disclaimer boilerplate — including cases with good OCR confidence (0.79–0.87), so this isn't purely an OCR problem.
- **OCR invocation itself can diverge** between pipelines on the identical page: Python's `has_selectable_text` check accepted 1 stray character as "enough" and skipped OCR entirely on one receipt; Node correctly OCR'd it and got the right answer. Worth fixing independently of OCR-quality work.

## Fields the current implementation doesn't have

- **Currency**: never recorded as its own field (amount parsing just requires a ₹/Rs./INR prefix to match, implicitly assumes INR).
- **Service/product description** vs. **category evidence**: conflated into one `description` field, which is actually the category-keyword snippet, not a line-item description.

## Untested this round

Deliberately-degraded/skewed scans, the opt-in NVIDIA page classifier (disabled, external — correctly not invoked), multi-receipt-per-page splitting (no real sample had one), and exhaustive field-level ground truth beyond the 13-sample set (143-page manual verification doesn't fit one diagnostic round).

## Recommended next fix, in priority order

1. **Extend the multi-transaction-statement guard to amount extraction**, not just bill grouping — the highest-confidence, highest-consequence finding (a wrong number accepted with no warning, on a document type already proven to occur in this exact corpus).
2. **Fix vendor extraction's adjacent-label problem** — second most common failure, affects both clean and noisy pages.
3. **Align the two pipelines' `has_selectable_text` OCR-trigger threshold** — a confirmed correctness gap that has nothing to do with OCR engine quality.

## Verification

Re-run fresh: `parity-test.mjs` 49/49, `parity-test-billparser.mjs` 100/100, `parity-test-textextraction.mjs` 10/10, `evaluate-parity.mjs` 154/154 — Expense Report extraction unchanged. All existing fixtures/annotations preserved. No file under `backend/` modified. Nothing staged or committed. No version bump, build, or publish.
