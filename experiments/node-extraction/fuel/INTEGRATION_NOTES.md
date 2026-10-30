# Notes for a later, separately authorised integration (nothing here is implemented in the backend)

**Phase 2: the matching safety rule below is CONFIRMED by the project owner and is now executable and tested in `src/matchSafety.mjs` / `tests/matchSafety.test.mjs` (experimental, not integrated): ambiguous, unreadable or insufficiently supported receipts never verify a claim; the claimed category is preserved; a fuel receipt alone never verifies an expense (`verifies_alone` is always false); amount and transaction date are the matching fields and pump/station name is supporting evidence only.**

Written from the brief; **the pasted brief was cut off in the middle of the sentence "If the amount or date is ambiguous, the receipt is unread..." in
section 11**, so the last rule below is my reading of the evident intent and should be confirmed.

## How a fuel receipt should be matched against an Expense Report claim

Inputs: the claim (category as claimed, claimed amount, claimed date) and the receipt's three-field result from this interface.

1. **Total amount** (primary evidence). Match only when `total_amount.normalized_value` exists, is not `review_required`, and equals the claim under the
   application's existing amount rule (exact, or whole-rupee rounding - never a wider tolerance). A receipt whose amount is null, ambiguous, repaired, fuzzy-labelled or conflicting is a *candidate*, never support.
2. **Transaction date** (second). Match only on a resolved `transaction_date.normalized_value` equal to the claim date. If the date is unresolved (`normalized_value === null`, both readings in `candidates`) it earns **no date credit** and must not be reported as a DATE MISMATCH either; it goes to review with both readings shown. The claim date must never be used to choose between the two readings.
3. **Pump / station name** (supporting evidence only). It can raise confidence when it agrees with the claim's vendor/description text and should be shown to the reviewer; its absence or a brand-only result is not a reason to reject an otherwise strong amount+date match, and its presence is never sufficient on its own.

## Category and verification rules that must stay as they are

* The category in the Expense Report remains authoritative. A receipt detected as fuel must **never** rewrite a claim's category and must not create a new category. A fuel receipt under a claim in another category is still a CATEGORY MISMATCH for that claim, as today.
* A detected petrol/diesel receipt **never verifies a claim by itself.** Verification requires the claimed amount and date to agree with resolved receipt evidence, and every existing policy check (limit, duplicate bill, category consistency, ...) still applies.
* If the amount or date is ambiguous, the receipt is unread, or the three-field result is `overall_review_required`, the claim is **not** auto-verified: keep the receipt as a visible candidate (with its evidence lines and warnings) and route to Review Required - consistent with the approved rule that an unreadable-amount bill never verifies a claim and that competing claims on one bill keep a visible duplicate/reuse review item.
* Do not call a hosted provider with the receipt; keep NVIDIA / DeepSeek disabled.

## Practical integration points (for whoever is authorised later)

* The existing `bill_parser.py` already produces vendor/date/amount; this extractor would be an additional, optional reader whose output is stored with its raw text, evidence and review flags, not a replacement of those columns.
* The new fields map to what already exists: `bill_date_raw / bill_date_candidates / bill_date_method / bill_date_confidence` (added in the generalisation work) for the date; `amount` for the total; `vendor` for the pump name.
* Before any production use: more authentic receipts (especially Nayara, Jio-bp, Shell and independent dealers), independent human verification of the labels, a calibration study if any threshold is to be treated as a probability, and a decision about the rotation/preprocessing retry's cost (it ran on N of the corpus images, see REPORT.md).
