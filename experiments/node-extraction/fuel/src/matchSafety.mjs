// EXPERIMENTAL matching-safety contract. NOT integrated into the backend; it documents and tests the rules a future integration must keep.
//
// Rules (confirmed by the project owner):
//   1. Ambiguous, unreadable or insufficiently supported receipts NEVER verify a claim automatically.
//   2. The Expense Report's claimed category is authoritative and is never rewritten or inferred from the receipt.
//   3. A fuel receipt alone NEVER verifies an expense: `verifies_alone` is always false; the application's own checks (policy limit,
//      duplicate bill, category consistency, ...) still decide.
//   4. Matching fields: total amount and transaction date. Pump/station name is SUPPORTING evidence only: it can add a note, it can
//      never create, upgrade or (alone) downgrade support.
//   5. An unresolved date earns no date credit - even when one candidate reading equals the claim date (that coincidence is not
//      evidence of the order) - and is not a date mismatch either.
//
// assessAgainstClaim(claim, extraction) never mutates its inputs and returns a plain object.

export const FUEL_COMPATIBLE_CATEGORIES = ["COMPANY CAR PETROL", "OWN CAR PETROL", "TWO WHEELER PETROL"]; // category_map.py maps exactly these to the 'Fuel' evidence class

// The application's amount rule (money.py amounts_match): exact, or - for a bill total that has paise - the claim equals that total rounded
// to either neighbouring whole rupee. No percentage tolerance.
export function amountsMatch(claimed, billAmount) {
  if (typeof claimed !== "number" || typeof billAmount !== "number") return false;
  const c = Math.round(claimed * 100), b = Math.round(billAmount * 100);
  if (c === b) return true;
  if (b % 100 === 0) return false; // an already whole-rupee bill only matches exactly
  return c === Math.floor(b / 100) * 100 || c === Math.ceil(b / 100) * 100;
}

const tokens = (s) => String(s ?? "").toUpperCase().split(/[^A-Z0-9]+/).filter((w) => w.length >= 4);

export function assessAgainstClaim(claim, extraction) {
  const reasons = [];
  const A = extraction.total_amount, D = extraction.transaction_date, P = extraction.pump_name;

  // ---- amount (primary)
  let amount;
  if (A.normalized_value === null || A.review_required) {
    amount = { status: "UNKNOWN", detail: A.normalized_value === null ? "No amount was established from the receipt." : `An amount (${A.normalized_value}) was read but is flagged for review, so it is not evidence.` };
  } else if (amountsMatch(claim.claimed_amount, A.normalized_value)) amount = { status: "MATCH", detail: `Claimed ${claim.claimed_amount} agrees with the receipt total ${A.normalized_value}.` };
  else amount = { status: "MISMATCH", detail: `Claimed ${claim.claimed_amount} differs from the receipt total ${A.normalized_value}.` };

  // ---- date (primary)
  let date;
  if (D.normalized_value === null) {
    const coincidence = Array.isArray(D.candidates) && D.candidates.includes(claim.expense_date);
    date = { status: "UNRESOLVED", detail: "The transaction date is not established." + (coincidence ? " One candidate reading equals the claim date, but that coincidence is not evidence of the day/month order, so it earns no credit." : ""), claim_date_is_one_candidate: coincidence };
  } else if (D.review_required) {
    date = { status: "UNRESOLVED", detail: `A date (${D.normalized_value}) was read but is flagged for review, so it is not evidence.` };
  } else if (D.normalized_value === claim.expense_date) date = { status: "MATCH", detail: `Transaction date ${D.normalized_value} equals the claim date.` };
  else date = { status: "MISMATCH", detail: `Transaction date ${D.normalized_value} differs from the claim date ${claim.expense_date}.` };

  // ---- pump / station (supporting only)
  const claimText = `${claim.details ?? ""} ${claim.vendor ?? ""}`;
  const shared = tokens(P?.normalized_value).filter((t) => tokens(claimText).includes(t));
  const pump = P?.normalized_value
    ? { status: shared.length ? "SUPPORTS" : "NOT_CORROBORATED", detail: shared.length ? `The receipt's pump name shares '${shared.join(", ")}' with the claim text.` : "The pump name is not corroborated by the claim text (this is only a note; it neither supports nor rejects).", review_required: Boolean(P.review_required) }
    : { status: "NOT_READ", detail: "No pump name was established (supporting evidence only).", review_required: true };

  // ---- evidence level (from amount and date ONLY)
  let level;
  if (amount.status === "MISMATCH" || date.status === "MISMATCH") level = "CONFLICT";
  else if (amount.status === "MATCH" && date.status === "MATCH") level = "STRONG";
  else if (amount.status === "MATCH" || date.status === "MATCH") level = "PARTIAL";
  else level = "INSUFFICIENT";
  reasons.push(amount.detail, date.detail);

  // ---- category: authoritative, never rewritten
  const compatible = FUEL_COMPATIBLE_CATEGORIES.includes(String(claim.category ?? "").toUpperCase());
  const category = { claimed: claim.category, rewritten: false, compatible_with_fuel_receipt: compatible,
    note: compatible ? "The claimed category is a fuel category." : "The claimed category is not a fuel category: the application's Category Consistency Check must raise this; the claim's category is not changed." };
  if (!compatible) reasons.push(category.note);

  const recommended = level === "CONFLICT" ? "EXCEPTION_CANDIDATE_MISMATCH" : level === "STRONG" && compatible ? "CANDIDATE_SUPPORT_REQUIRES_APPLICATION_CHECKS" : "REVIEW_REQUIRED";
  return {
    claim_category: claim.category, // echoed unchanged
    evidence_level: level,
    amount, date, pump, category,
    verifies_alone: false,
    auto_verification: "never: the application's own checks (policy limit, duplicate bill, category consistency, ...) still decide",
    recommended_status: recommended,
    review_required: recommended !== "CANDIDATE_SUPPORT_REQUIRES_APPLICATION_CHECKS",
    reasons,
  };
}
