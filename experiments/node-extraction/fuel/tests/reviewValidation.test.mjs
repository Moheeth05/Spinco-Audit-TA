// Unit tests of the pure validation functions in src/reviewValidation.mjs. Synthetic inputs only; no files are read or written.
import test from "node:test";
import assert from "node:assert/strict";
import { normalizeReviewerId, validateReviewerId, validateStoredReviewerId, reviewerKey, isRealIsoDate, validateReviewFields, agreementKey, fieldsInDisagreement } from "../src/reviewValidation.mjs";

const good = () => ({
  pump_name: { status: "read", value: "SYNTH FUELS" },
  transaction_date: { status: "read", iso_date: "2026-09-24", order_provable_from_receipt: true, order_evidence: "component_gt_12" },
  total_amount: { status: "read", value: 100 },
});

test("reviewer id: normalisation and the documented length / character rule", () => {
  assert.equal(normalizeReviewerId("  Asha    K "), "Asha K");
  assert.equal(normalizeReviewerId("abc"), "abc");
  assert.equal(normalizeReviewerId("ab"), null);
  assert.equal(normalizeReviewerId("a".repeat(64)), "a".repeat(64));
  assert.equal(normalizeReviewerId("a".repeat(65)), null);
  for (const bad of ["", "   ", "x", "a+b", "a/b", "ünï-code", "-abc", "abc-", "abc.", ".abc", 7, null, undefined, {}]) assert.equal(normalizeReviewerId(bad), null, String(bad));
});
test("reviewer identity key: case-insensitive on the normalised id, so a case variant is the same person; different spellings stay different", () => {
  assert.equal(reviewerKey(normalizeReviewerId("REV-001")), reviewerKey(normalizeReviewerId(" rev-001 ")));
  assert.notEqual(reviewerKey(normalizeReviewerId("REV-001")), reviewerKey(normalizeReviewerId("REV-0010")));
  assert.notEqual(reviewerKey(normalizeReviewerId("j smith")), reviewerKey(normalizeReviewerId("j-smith")));
  assert.equal(validateReviewerId("x").ok, false);
  assert.equal(validateReviewerId("REV-001").ok, true);
});
test("stored (composite) reviewer ids must consist of distinct well-formed ids", () => {
  assert.equal(validateStoredReviewerId("REV-001"), true);
  assert.equal(validateStoredReviewerId("REV-001+REV-002"), true);
  assert.equal(validateStoredReviewerId("REV-001+rev-001"), false);
  assert.equal(validateStoredReviewerId("REV-001+x"), false);
  assert.equal(validateStoredReviewerId(""), false);
  assert.equal(validateStoredReviewerId(5), false);
});
test("calendar dates: leap days, impossible days and months, formats", () => {
  for (const ok of ["2024-02-29", "2000-02-29", "2026-02-28", "2026-04-30", "2026-12-31"]) assert.equal(isRealIsoDate(ok), true, ok);
  for (const bad of ["2025-02-29", "2100-02-29", "2026-02-30", "2026-04-31", "2026-13-01", "2026-00-01", "2026-01-00", "2026-01-32", "2026-1-5", "26-01-05", "01-05-2026", "2026/01/05", "20260105", "2026-01-05T00:00:00Z", "", null, undefined, 20260105, "0026-09-24", "2101-01-01"]) assert.equal(isRealIsoDate(bad), false, String(bad));
});
test("field validation: a fully valid entry passes; each documented inconsistency is reported by field", () => {
  assert.deepEqual(validateReviewFields(good()), []);
  const only = (patch, field) => { const e = good(); e[field] = { ...e[field], ...patch }; return validateReviewFields(e); };
  assert.match(only({ status: "banana" }, "pump_name").join(), /pump_name: unsupported status/);
  assert.match(only({ value: null }, "pump_name").join(), /pump_name: .*requires a non-empty value/);
  assert.match(only({ status: "not_printed" }, "pump_name").join(), /contradicts/);
  assert.match(only({ iso_date: "2026-02-30" }, "transaction_date").join(), /real calendar date/);
  assert.match(only({ order_provable_from_receipt: undefined }, "transaction_date").join(), /true or false/);
  assert.match(only({ value: "100" }, "total_amount").join(), /numeric/);
  assert.match(only({ status: "illegible" }, "total_amount").join(), /contradicts/);
  assert.match(validateReviewFields({ ...good(), notes: { a: 1 } }).join(), /notes/);
  assert.deepEqual(validateReviewFields({ ...good(), total_amount: { status: "illegible", value: null } }), []);
  assert.equal(validateReviewFields(null).length, 1);
  assert.equal(validateReviewFields({}).length, 3);
});
test("agreement key covers status, value and date provability of every required field", () => {
  const a = good(), b = good();
  assert.deepEqual(fieldsInDisagreement(a, b), []);
  b.transaction_date = { ...b.transaction_date, order_provable_from_receipt: false, order_evidence: "none" };
  assert.deepEqual(fieldsInDisagreement(a, b), ["transaction_date"]);
  const c = good(); c.pump_name = { status: "logo_only", value: "SYNTH FUELS" };
  assert.deepEqual(fieldsInDisagreement(a, c), ["pump_name"]);
  const d = good(); d.total_amount = { status: "read", value: 101 }; d.pump_name = { status: "read", value: "synth  fuels" };
  assert.deepEqual(fieldsInDisagreement(a, d), ["total_amount"], "pump comparison ignores case and punctuation, as before");
  assert.equal(agreementKey(a).transaction_date.order_provable_from_receipt, true);
});
