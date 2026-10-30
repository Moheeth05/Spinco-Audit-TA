// Hardening tests for the human-review EXPORT -> verified-label path (scripts/apply_verification.mjs, src/labels.mjs overlay loading).
// Black-box: every test runs the real script against a throw-away sandbox in the OS temp directory with SYNTHETIC fixtures.
// Nothing here reads or writes the real corpus, the real review pack, the policy file or any report, and no OCR / extraction / scoring runs.
import test from "node:test";
import assert from "node:assert/strict";
import crypto from "node:crypto";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import { spawnSync } from "node:child_process";
import { fileURLToPath } from "node:url";
import { loadAnnotations, applyReviewerEntry } from "../src/labels.mjs";

const FUEL = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const APPLY = path.join(FUEL, "scripts", "apply_verification.mjs");
const SECRET = "SYNTHETIC-SECRET-NAME-77"; // must never be echoed in an error message

const rec = (id, pump, date, amt) => ({
  receipt_id: id, source_type: "authentic", receipt_family: "synthetic_fam", split: "dev", kind: "fuel", annotation_status: "provisional_assistant_read",
  fields: {
    pump_name: { raw_value: pump, normalized_value: pump, acceptable_forms: [pump], review_required: false },
    transaction_date: { raw_value: date, normalized_value: date, review_required: false, ambiguous_on_receipt_alone: false },
    total_amount: { raw_value: String(amt), normalized_value: amt, review_required: false },
  },
});
function sandbox(policy = { assistant_read_labels: "provisional_included" }) {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), "fuel-harden-"));
  for (const d of ["corpus", "verification", "in"]) fs.mkdirSync(path.join(dir, d), { recursive: true });
  fs.writeFileSync(path.join(dir, "corpus", "annotations.json"), JSON.stringify({ records: [rec("a1", "SYNTH FUELS", "2026-09-24", 100), rec("a2", "TEST SERVICE", "2026-08-25", 200)] }));
  fs.writeFileSync(path.join(dir, "verification", "label_policy.json"), JSON.stringify(policy));
  return dir;
}
const good = () => ({
  pump_name: { status: "read", value: "SYNTH FUELS", printed_text: "SYNTH FUELS" },
  transaction_date: { status: "read", printed_text: "24/09/26", iso_date: "2026-09-24", order_provable_from_receipt: true, order_evidence: "component_gt_12" },
  total_amount: { status: "read", printed_text: "100.00", value: 100 },
});
const exp = (reviewer_id, records, extra = {}) => ({ schema: "fuel-review-export/v1", reviewer_id, authorization_confirmed: true, exported_at: "2026-10-22T10:00:00Z", records, ...extra });
let n = 0;
function apply(dir, exports_) {
  const files = exports_.map((e) => { const f = path.join(dir, "in", `e${++n}.json`); fs.writeFileSync(f, JSON.stringify(e)); return f; });
  const r = spawnSync("node", [APPLY, ...files, "--annotations-dir", dir], { encoding: "utf8" });
  return { status: r.status, out: r.stdout, err: r.stderr, all: r.stdout + r.stderr };
}
const vf = (dir) => path.join(dir, "corpus", "verified_labels.json");
const readJson = (f) => JSON.parse(fs.readFileSync(f, "utf8"));
const sha = (f) => crypto.createHash("sha256").update(fs.readFileSync(f)).digest("hex");
const verifiedIds = (dir) => (fs.existsSync(vf(dir)) ? Object.keys(readJson(vf(dir)).records) : []);
function assertRejected(dir, res, re, label) {
  assert.notEqual(res.status, 0, `${label}: must exit non-zero`);
  assert.match(res.all, re, `${label}: message`);
  assert.deepEqual(verifiedIds(dir), [], `${label}: nothing may be written as verified`);
  assert.equal(loadAnnotations(dir).summary.human_verified, 0, `${label}: nothing may load as human_verified`);
}
const withField = (field, patch) => ({ ...good(), [field]: { ...good()[field], ...patch } });

// ---- A. reviewer identity -------------------------------------------------------------------------------------------------------
test("A: blank, whitespace-only, one-character, over-long, malformed and non-string reviewer ids are rejected", () => {
  for (const id of ["", "   ", "\t\n", "x", "ab", "a".repeat(65), "bad/id", "a+b", "ann<script>", "-lead", "trail.", 12345, null]) {
    const dir = sandbox();
    assertRejected(dir, apply(dir, [exp(id, { a1: good() })]), /reviewer_id/, `id=${JSON.stringify(id)}`);
  }
});
test("A: valid ids are accepted; internal whitespace is normalised; the stored identity is the normalised id", () => {
  for (const [id, stored] of [["REV-001", "REV-001"], ["  Asha   K  ", "Asha K"], ["j.smith@example.test", "j.smith@example.test"], ["a".repeat(64), "a".repeat(64)]]) {
    const dir = sandbox();
    const r = apply(dir, [exp(id, { a1: good() })]);
    assert.equal(r.status, 0, `${id}: ${r.all}`);
    assert.equal(readJson(vf(dir)).records.a1.reviewer_id, stored);
  }
});

// ---- B. status / value consistency ----------------------------------------------------------------------------------------------
test("B: unsupported statuses are rejected for every field and never leave a provisional value in place", () => {
  for (const [field, status] of [["pump_name", "banana"], ["pump_name", "unread"], ["transaction_date", "uncertain"], ["transaction_date", "logo_only"], ["total_amount", "not_printed"], ["total_amount", "banana"], ["pump_name", null], ["total_amount", undefined]]) {
    const dir = sandbox();
    assertRejected(dir, apply(dir, [exp("REV-001", { a1: withField(field, { status }) })]), /status/, `${field}=${status}`);
  }
});
test("B: a field without an object is rejected (a missing field cannot be promoted to verified)", () => {
  for (const f of ["pump_name", "transaction_date", "total_amount"]) {
    const dir = sandbox();
    const e = good(); delete e[f];
    assertRejected(dir, apply(dir, [exp("REV-001", { a1: e })]), new RegExp(f), `missing ${f}`);
  }
});
test("B: a 'read' status with a null, empty, mistyped or implausible value is rejected", () => {
  const bad = [
    ["pump_name", { value: null }], ["pump_name", { value: "   " }], ["pump_name", { value: 42 }], ["pump_name", { status: "logo_only", value: null }],
    ["total_amount", { value: null }], ["total_amount", { value: "100" }], ["total_amount", { value: 0 }], ["total_amount", { value: -5 }], ["total_amount", { value: Number.NaN }],
    ["total_amount", { value: 100.123 }], ["total_amount", { value: 1e12 }],
    ["transaction_date", { iso_date: null }], ["transaction_date", { iso_date: "" }],
  ];
  for (const [field, patch] of bad) {
    const dir = sandbox();
    assertRejected(dir, apply(dir, [exp("REV-001", { a1: withField(field, patch) })]), new RegExp(field), `${field} ${JSON.stringify(patch)}`);
  }
});
test("B: 'illegible' / 'not_printed' with a value present is contradictory and rejected; without a value they are accepted and excluded", () => {
  for (const [field, patch] of [["pump_name", { status: "illegible" }], ["pump_name", { status: "not_printed" }], ["transaction_date", { status: "illegible" }], ["total_amount", { status: "illegible" }]]) {
    const dir = sandbox();
    assertRejected(dir, apply(dir, [exp("REV-001", { a1: withField(field, patch) })]), new RegExp(field), `${field} ${patch.status} with value`);
  }
  const dir = sandbox();
  const e = { pump_name: { status: "not_printed", value: null }, transaction_date: { status: "illegible", iso_date: null, order_provable_from_receipt: false }, total_amount: { status: "illegible", value: null } };
  const r = apply(dir, [exp("REV-001", { a1: e })]);
  assert.equal(r.status, 0, r.all);
  const a1 = loadAnnotations(dir).records.find((x) => x.receipt_id === "a1");
  assert.equal(a1.annotation_status, "human_verified");
  assert.equal(a1.fields.transaction_date.excluded, true);
  assert.equal(a1.fields.total_amount.excluded, true);
  assert.equal(a1.fields.pump_name.normalized_value, null);
});
test("B: errors name the receipt and field but never echo the reviewer's values", () => {
  const dir = sandbox();
  const res = apply(dir, [exp("REV-001", { a1: withField("pump_name", { status: "banana", value: SECRET, printed_text: SECRET }) })]);
  assert.notEqual(res.status, 0);
  assert.ok(!res.all.includes(SECRET), "a value from the export leaked into the error output");
  assert.match(res.all, /a1/);
  assert.match(res.all, /pump_name/);
});
test("B: one invalid receipt rejects the whole export - there is no partial application", () => {
  const dir = sandbox();
  const res = apply(dir, [exp("REV-001", { a1: good(), a2: withField("total_amount", { value: null }) })]);
  assertRejected(dir, res, /a2/, "mixed export");
});

// ---- C. calendar dates ----------------------------------------------------------------------------------------------------------
test("C: real calendar dates are accepted, including a genuine leap day", () => {
  for (const d of ["2026-09-24", "2024-02-29", "2028-02-29", "2026-12-31", "2026-01-01"]) {
    const dir = sandbox();
    const r = apply(dir, [exp("REV-001", { a1: withField("transaction_date", { iso_date: d, order_provable_from_receipt: true, order_evidence: "month_name_or_iso" }) })]);
    assert.equal(r.status, 0, `${d}: ${r.all}`);
  }
});
test("C: impossible dates, non-leap Feb 29, wrong separators / ambiguous formats, timestamps and implausible years are rejected", () => {
  const bad = ["2026-13-45", "2026-02-30", "2025-02-29", "2100-02-29", "2026-00-10", "2026-04-31", "2026-06-00", "2026-09-32",
    "24/09/2026", "09/24/2026", "2026/09/24", "2026-9-4", "20260924", "09-24-2026", "24-09-2026", "2026-09-24T00:00:00Z", " 2026-09-24", "2026-09-24 ", "0026-09-24", "1800-01-01", "3026-09-24"];
  for (const d of bad) {
    const dir = sandbox();
    assertRejected(dir, apply(dir, [exp("REV-001", { a1: withField("transaction_date", { iso_date: d, order_provable_from_receipt: true, order_evidence: "month_name_or_iso" }) })]), /transaction_date/, `date ${JSON.stringify(d)}`);
  }
});
test("C: a missing date is only acceptable as 'illegible' (excluded); the date is never inferred from anything else", () => {
  const dir = sandbox();
  const e = withField("transaction_date", { status: "illegible", iso_date: null, order_provable_from_receipt: false, order_evidence: "none" });
  const r = apply(dir, [exp("REV-001", { a1: e })]);
  assert.equal(r.status, 0, r.all);
  assert.equal(loadAnnotations(dir).records.find((x) => x.receipt_id === "a1").fields.transaction_date.excluded, true);
});

// ---- D. independent-review integrity ---------------------------------------------------------------------------------------------
test("D: the same reviewer submitting twice (even with different case / spacing) is rejected and never counts as independent agreement", () => {
  for (const [a, b] of [["REV-001", "REV-001"], ["REV-001", "rev-001"], ["Asha K", "  asha   k "]]) {
    const dir = sandbox();
    assertRejected(dir, apply(dir, [exp(a, { a1: good() }), exp(b, { a1: good() })]), /duplicate|same reviewer|independent/i, `${a} / ${b}`);
  }
});
test("D: two distinct reviewers who agree qualify as independent agreement; one reviewer is recorded as NOT second-reviewed", () => {
  const dir = sandbox();
  assert.equal(apply(dir, [exp("REV-001", { a1: good() }), exp("REV-002", { a1: good() })]).status, 0);
  const v = readJson(vf(dir)).records.a1;
  assert.equal(v.second_reviewer_agreed, true);
  assert.equal(v.reviewer_count, 2);
  const dir2 = sandbox();
  assert.equal(apply(dir2, [exp("REV-001", { a1: good() })]).status, 0);
  const v2 = readJson(vf(dir2)).records.a1;
  assert.equal(v2.second_reviewer_agreed, false);
  assert.equal(v2.reviewer_count, 1);
});
test("D: two distinct reviewers who disagree produce no verified label and an explicit adjudication record", () => {
  const dir = sandbox();
  const r = apply(dir, [exp("REV-001", { a1: good() }), exp("REV-002", { a1: withField("total_amount", { value: 999 }) })]);
  assert.equal(r.status, 0, r.all);
  assert.deepEqual(verifiedIds(dir), []);
  assert.equal(loadAnnotations(dir).summary.human_verified, 0);
  const adj = readJson(path.join(dir, "corpus", "adjudication_needed.json"));
  assert.equal(adj.a1.length, 2);
});

// ---- E. cross-field agreement / eligibility --------------------------------------------------------------------------------------
test("E: reviewers who agree on date and amount but disagree on whether the day/month order is provable are NOT verified", () => {
  const dir = sandbox();
  const r = apply(dir, [exp("REV-001", { a1: good() }), exp("REV-002", { a1: withField("transaction_date", { order_provable_from_receipt: false, order_evidence: "none" }) })]);
  assert.equal(r.status, 0, r.all);
  assert.deepEqual(verifiedIds(dir), []);
  assert.ok(readJson(path.join(dir, "corpus", "adjudication_needed.json")).a1);
});
test("E: reviewers who agree on values but disagree on the pump status (printed name vs logo only) are NOT verified", () => {
  const dir = sandbox();
  apply(dir, [exp("REV-001", { a1: good() }), exp("REV-002", { a1: withField("pump_name", { status: "logo_only" }) })]);
  assert.deepEqual(verifiedIds(dir), []);
});
test("E: a date whose provability claim contradicts its evidence is rejected (value must be consistent with evidence)", () => {
  const cases = [
    { order_provable_from_receipt: true, order_evidence: "none" },
    { order_provable_from_receipt: true, order_evidence: "other_documents" },
    { order_provable_from_receipt: false, order_evidence: "component_gt_12" },
    { order_provable_from_receipt: false, order_evidence: "month_name_or_iso" },
    { order_provable_from_receipt: true, order_evidence: "nonsense" },
    { order_provable_from_receipt: "yes", order_evidence: "component_gt_12" },
    { iso_date: "2026-09-05", order_provable_from_receipt: true, order_evidence: "component_gt_12" },  // neither component > 12
    { iso_date: "2026-09-05", order_provable_from_receipt: true, order_evidence: "equal_components" }, // 5 != 9
  ];
  for (const patch of cases) {
    const dir = sandbox();
    assertRejected(dir, apply(dir, [exp("REV-001", { a1: withField("transaction_date", patch) })]), /transaction_date/, JSON.stringify(patch));
  }
});
test("E: evidence that IS consistent with the date is accepted (equal components, component > 12, an unprovable order with no evidence)", () => {
  for (const patch of [
    { iso_date: "2026-09-09", order_provable_from_receipt: true, order_evidence: "equal_components" },
    { iso_date: "2026-09-24", order_provable_from_receipt: true, order_evidence: "component_gt_12" },
    { iso_date: "2026-09-05", order_provable_from_receipt: false, order_evidence: "none" },
    { iso_date: "2026-09-05", order_provable_from_receipt: false, order_evidence: "" },
  ]) {
    const dir = sandbox();
    const r = apply(dir, [exp("REV-001", { a1: withField("transaction_date", patch) })]);
    assert.equal(r.status, 0, `${JSON.stringify(patch)}: ${r.all}`);
  }
});

// ---- F. no silent overwrite -----------------------------------------------------------------------------------------------------
test("F: R1 says 100, a LATER R2 says 999 for the same receipt: R1's record is untouched, the conflict is recorded, and the label is not verified", () => {
  const dir = sandbox();
  assert.equal(apply(dir, [exp("REV-R1", { a1: good() })]).status, 0);
  const before = sha(vf(dir));
  const r2 = apply(dir, [exp("REV-R2", { a1: withField("total_amount", { value: 999 }) })]);
  assert.notEqual(r2.status, 0, "a conflicting later review must exit non-zero");
  assert.match(r2.all, /conflict/i);
  assert.equal(sha(vf(dir)), before, "the earlier verified_labels.json must not be rewritten");
  assert.equal(readJson(vf(dir)).records.a1.total_amount.value, 100);
  const conflicts = readJson(path.join(dir, "corpus", "review_conflicts.json"));
  assert.equal(conflicts.events.length, 1);
  assert.equal(conflicts.events[0].status, "open");
  assert.equal(conflicts.events[0].receipt_id, "a1");
  assert.deepEqual(conflicts.events[0].fields_in_conflict, ["total_amount"]);
  const { summary, records } = loadAnnotations(dir);
  assert.equal(summary.human_verified, 0, "a receipt with an open conflict must not load as human_verified");
  assert.equal(summary.held_by_open_conflict, 1);
  assert.equal(records.find((x) => x.receipt_id === "a1").annotation_status, "provisional_assistant_read");
});
test("F: re-applying the identical review is a no-op (no rewrite, no conflict); a disagreeing pair for an already-verified receipt is also a conflict", () => {
  const dir = sandbox();
  apply(dir, [exp("REV-R1", { a1: good() })]);
  const before = sha(vf(dir));
  const again = apply(dir, [exp("REV-R1", { a1: good() })]);
  assert.equal(again.status, 0, again.all);
  assert.equal(sha(vf(dir)), before);
  assert.ok(!fs.existsSync(path.join(dir, "corpus", "review_conflicts.json")));
  const pair = apply(dir, [exp("REV-R2", { a1: good() }), exp("REV-R3", { a1: withField("total_amount", { value: 999 }) })]);
  assert.notEqual(pair.status, 0);
  assert.equal(sha(vf(dir)), before);
  assert.equal(readJson(path.join(dir, "corpus", "review_conflicts.json")).events.length, 1);
});
test("F: other receipts in the same run are still applied; adjudication and conflict records accumulate instead of being replaced", () => {
  const dir = sandbox();
  apply(dir, [exp("REV-R1", { a1: good() })]);
  const r = apply(dir, [exp("REV-R2", { a1: withField("total_amount", { value: 999 }), a2: { ...good(), pump_name: { status: "read", value: "TEST SERVICE" }, transaction_date: { status: "read", iso_date: "2026-08-25", order_provable_from_receipt: true, order_evidence: "component_gt_12" }, total_amount: { status: "read", value: 200 } } })]);
  assert.notEqual(r.status, 0);
  assert.deepEqual(verifiedIds(dir).sort(), ["a1", "a2"], "the non-conflicting receipt is applied, the conflicting one is left as it was");
  assert.equal(readJson(vf(dir)).records.a1.total_amount.value, 100);
  // adjudication file: a second run must not drop the first run's entries
  const dir2 = sandbox();
  apply(dir2, [exp("REV-R1", { a1: good() }), exp("REV-R2", { a1: withField("total_amount", { value: 999 }) })]);
  apply(dir2, [exp("REV-R3", { a2: { ...good(), total_amount: { status: "read", value: 200 } } }), exp("REV-R4", { a2: { ...good(), total_amount: { status: "read", value: 201 } } })]);
  const adj = readJson(path.join(dir2, "corpus", "adjudication_needed.json"));
  assert.ok(adj.a1 && adj.a2, "earlier adjudication evidence must be preserved");
});
test("F: every successful application is appended to an audit history; earlier entries are never replaced", () => {
  const dir = sandbox();
  apply(dir, [exp("REV-R1", { a1: good() })]);
  apply(dir, [exp("REV-R2", { a2: { ...good(), pump_name: { status: "read", value: "TEST SERVICE" }, transaction_date: { status: "read", iso_date: "2026-08-25", order_provable_from_receipt: true, order_evidence: "component_gt_12" }, total_amount: { status: "read", value: 200 } } })]);
  const h = readJson(path.join(dir, "corpus", "review_history.json"));
  assert.equal(h.events.length, 2);
  assert.deepEqual(h.events[0].exports.map((x) => x.reviewer_id), ["REV-R1"]);
  assert.deepEqual(h.events[1].exports.map((x) => x.reviewer_id), ["REV-R2"]);
  assert.match(h.events[0].exports[0].sha256, /^[0-9a-f]{64}$/);
});

// ---- G. authorization limitations -----------------------------------------------------------------------------------------------
test("G: authorization is self-attested and the allowlist is NOT enforced; the tool says so, and the policy file is untouched", () => {
  const dir = sandbox({ assistant_read_labels: "provisional_included", authorized_reviewers: ["SOMEONE-ELSE"] });
  const policyBefore = sha(path.join(dir, "verification", "label_policy.json"));
  const r = apply(dir, [exp("REV-001", { a1: good() })]);
  assert.equal(r.status, 0, r.all);
  const summary = JSON.parse(r.out);
  assert.equal(summary.reviewer_identity_assurance, "self_attested");
  assert.equal(summary.authorized_reviewers_enforced, false);
  const v = readJson(vf(dir)).records.a1;
  assert.equal(v.identity_assurance, "self_attested");
  assert.equal(sha(path.join(dir, "verification", "label_policy.json")), policyBefore);
});
test("G: an export without authorization_confirmed === true is still rejected (the only enforced authorization check)", () => {
  for (const auth of [false, "true", 1, undefined]) {
    const dir = sandbox();
    assertRejected(dir, apply(dir, [exp("REV-001", { a1: good() }, { authorization_confirmed: auth })]), /authorization_confirmed/, `auth=${auth}`);
  }
});

// ---- loader defence in depth ----------------------------------------------------------------------------------------------------
test("loader: a hand-written overlay entry with an invalid status, date or reviewer id is ignored and counted, never trusted", () => {
  const e = (patch = {}) => ({ reviewer_id: "REV-001", authorization_confirmed: true, verified_at: "2026-10-22T10:00:00Z", ...good(), ...patch });
  const dir = sandbox();
  fs.writeFileSync(vf(dir), JSON.stringify({ records: {
    a1: e({ pump_name: { status: "banana", value: "X" } }),
    a2: e({ transaction_date: { status: "read", iso_date: "2026-13-45", order_provable_from_receipt: true } }),
  } }));
  let { summary } = loadAnnotations(dir);
  assert.equal(summary.human_verified, 0);
  assert.equal(summary.ignored_invalid_overlay_entries, 2);
  fs.writeFileSync(vf(dir), JSON.stringify({ records: { a1: e({ reviewer_id: "x" }), a2: e({ reviewer_id: "ab+cd" }) } }));
  ({ summary } = loadAnnotations(dir));
  assert.equal(summary.human_verified, 0);
  assert.equal(summary.ignored_invalid_overlay_entries, 2);
});
test("loader: applyReviewerEntry refuses an invalid reviewer entry instead of silently keeping the provisional value", () => {
  assert.throws(() => applyReviewerEntry(rec("a1", "SYNTH FUELS", "2026-09-24", 100), { reviewer_id: "REV-001", verified_at: "2026-10-22T10:00:00Z", authorization_confirmed: true, ...good(), pump_name: { status: "banana", value: "X" } }), /status/);
});
