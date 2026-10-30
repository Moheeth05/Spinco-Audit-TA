import test from "node:test";
import assert from "node:assert/strict";
import { assessAgainstClaim, amountsMatch } from "../src/matchSafety.mjs";

const field = (v, review = false, extra = {}) => ({ raw_value: v === null ? null : String(v), normalized_value: v, confidence: 0.9, confidence_type: "heuristic_uncalibrated", evidence: [], ambiguity: null, review_required: review, ...extra });
const extraction = ({ pump = "KHYBER SERVICE STATION", date = "2026-09-24", amount = 3528.45, pumpReview = false, dateReview = false, amountReview = false, candidates } = {}) => ({
  pump_name: field(pump, pumpReview), transaction_date: field(date, dateReview, candidates ? { candidates } : {}), total_amount: field(amount, amountReview), validation_warnings: [], overall_review_required: false,
});
const claim = (o = {}) => ({ category: "OWN CAR PETROL", claimed_amount: 3528.45, expense_date: "2026-09-24", details: "Petrol for site visit", ...o });

test("a fuel receipt with matching amount and resolved date is only a CANDIDATE: it never verifies alone", () => {
  const r = assessAgainstClaim(claim(), extraction());
  assert.equal(r.evidence_level, "STRONG");
  assert.equal(r.verifies_alone, false);
  assert.equal(r.recommended_status, "CANDIDATE_SUPPORT_REQUIRES_APPLICATION_CHECKS");
  assert.match(r.auto_verification, /never/);
});

test("verifies_alone is false in every case, including a perfect receipt with every field confident", () => {
  for (const [c, e] of [[claim(), extraction()], [claim({ claimed_amount: 1 }), extraction()], [claim(), extraction({ amount: null })], [claim({ category: "LODGING" }), extraction()]]) {
    assert.equal(assessAgainstClaim(c, e).verifies_alone, false);
  }
});

test("an unresolved (ambiguous) date earns no credit even when one reading equals the claim date, and is not a mismatch", () => {
  const r = assessAgainstClaim(claim({ expense_date: "2026-07-02" }), extraction({ date: null, candidates: ["2026-07-02", "2026-02-07"], amount: 3528.45 }));
  assert.equal(r.date.status, "UNRESOLVED");
  assert.equal(r.date.claim_date_is_one_candidate, true);
  assert.match(r.date.detail, /not evidence/);
  assert.equal(r.evidence_level, "PARTIAL");
  assert.equal(r.recommended_status, "REVIEW_REQUIRED");
  assert.notEqual(r.evidence_level, "CONFLICT");
});

test("a date that was read but is flagged for review is not evidence either", () => {
  const r = assessAgainstClaim(claim(), extraction({ dateReview: true }));
  assert.equal(r.date.status, "UNRESOLVED");
  assert.equal(r.recommended_status, "REVIEW_REQUIRED");
});

test("an unreadable or review-flagged amount can never support a claim", () => {
  assert.equal(assessAgainstClaim(claim(), extraction({ amount: null })).amount.status, "UNKNOWN");
  const flagged = assessAgainstClaim(claim(), extraction({ amountReview: true }));
  assert.equal(flagged.amount.status, "UNKNOWN");
  assert.equal(flagged.recommended_status, "REVIEW_REQUIRED");
  assert.equal(assessAgainstClaim(claim(), extraction({ amount: null, dateReview: true })).evidence_level, "INSUFFICIENT");
});

test("a resolved date or amount that disagrees is a CONFLICT (an exception candidate), never support", () => {
  assert.equal(assessAgainstClaim(claim(), extraction({ date: "2026-09-25" })).evidence_level, "CONFLICT");
  assert.equal(assessAgainstClaim(claim(), extraction({ amount: 3000 })).evidence_level, "CONFLICT");
  assert.equal(assessAgainstClaim(claim(), extraction({ amount: 3000 })).recommended_status, "EXCEPTION_CANDIDATE_MISMATCH");
});

test("the claimed category is authoritative: never rewritten, and a non-fuel category is flagged for the application's category check", () => {
  const c = claim({ category: "LODGING" });
  const snapshot = JSON.stringify(c);
  const r = assessAgainstClaim(c, extraction());
  assert.equal(JSON.stringify(c), snapshot, "inputs are not mutated");
  assert.equal(r.claim_category, "LODGING");
  assert.equal(r.category.rewritten, false);
  assert.equal(r.category.compatible_with_fuel_receipt, false);
  assert.equal(r.recommended_status, "REVIEW_REQUIRED");
  assert.match(r.category.note, /Category Consistency Check/);
});

test("pump / station name is supporting evidence only: it cannot create or upgrade support", () => {
  const nothingElse = assessAgainstClaim(claim({ details: "KHYBER SERVICE STATION fuel" }), extraction({ amount: null, date: null }));
  assert.equal(nothingElse.pump.status, "SUPPORTS");
  assert.equal(nothingElse.evidence_level, "INSUFFICIENT");
  assert.equal(nothingElse.recommended_status, "REVIEW_REQUIRED");
  const partial = assessAgainstClaim(claim({ details: "KHYBER SERVICE STATION fuel" }), extraction({ date: null }));
  assert.equal(partial.evidence_level, "PARTIAL", "a corroborating name does not turn PARTIAL into STRONG");
});

test("a pump name that is not corroborated does not downgrade otherwise strong evidence", () => {
  const r = assessAgainstClaim(claim({ details: "Petrol" }), extraction({ pump: "SOMEBODY ELSE FUELS" }));
  assert.equal(r.pump.status, "NOT_CORROBORATED");
  assert.equal(r.evidence_level, "STRONG");
});

test("amount rule matches the application's: exact or a neighbouring whole rupee of a bill total that has paise; no percentage tolerance", () => {
  assert.equal(amountsMatch(3528.45, 3528.45), true);
  assert.equal(amountsMatch(3528, 3528.45), true);
  assert.equal(amountsMatch(3529, 3528.45), true);
  assert.equal(amountsMatch(3527, 3528.45), false);
  assert.equal(amountsMatch(2001, 2000), false, "a whole-rupee bill matches only exactly");
  assert.equal(amountsMatch(1990, 2000), false, "no percentage tolerance");
});
