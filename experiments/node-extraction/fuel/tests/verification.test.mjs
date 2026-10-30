// Tests for the human-verification workflow. All fixtures live in a temp directory; nothing here writes to the real corpus,
// and no test can mark a real label verified.
import test from "node:test";
import assert from "node:assert/strict";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import { execFileSync } from "node:child_process";
import { fileURLToPath } from "node:url";
import { loadAnnotations, applyReviewerEntry, overlayEntryIsValid } from "../src/labels.mjs";
import { scoreReceipt, aggregate } from "../src/scoring.mjs";

const FUEL = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const rec = (id, source_type, status, pump, date, amt) => ({
  receipt_id: id, source_type, receipt_family: "fam", split: source_type === "authentic" ? "dev" : "heldout", kind: "fuel", annotation_status: status,
  fields: {
    pump_name: { raw_value: pump, normalized_value: pump, acceptable_forms: pump ? [pump] : [], review_required: false },
    transaction_date: { raw_value: date, normalized_value: date, review_required: false, ambiguous_on_receipt_alone: false },
    total_amount: { raw_value: String(amt), normalized_value: amt, review_required: false },
  },
});
function sandbox() {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), "fuel-verif-"));
  fs.mkdirSync(path.join(dir, "corpus"), { recursive: true });
  fs.mkdirSync(path.join(dir, "verification"), { recursive: true });
  fs.writeFileSync(path.join(dir, "corpus", "annotations.json"), JSON.stringify({ records: [rec("a1", "authentic", "provisional_assistant_read", "KHYBER SERVICE STATION", "2026-09-24", 3528.45), rec("a2", "authentic", "provisional_assistant_read", "ONKAR SERVICE", "2026-08-25", 4762.93), rec("s1", "synthetic", "construction_truth", "PATEL FUEL", "2026-07-01", 500)] }));
  fs.writeFileSync(path.join(dir, "verification", "label_policy.json"), JSON.stringify({ assistant_read_labels: "provisional_included" }));
  return dir;
}
const goodEntry = (extra = {}) => ({
  reviewer_id: "TEST-REVIEWER-1", authorization_confirmed: true, verified_at: "2026-10-21T10:00:00Z",
  pump_name: { status: "read", value: "KHYBER SERVICE STATION", printed_text: "KHYBER SERVICE STATION" },
  transaction_date: { status: "read", printed_text: "24/09/26", iso_date: "2026-09-24", order_provable_from_receipt: true, order_evidence: "component_gt_12" },
  total_amount: { status: "read", printed_text: "03528.45", value: 3528.45 }, ...extra,
});

test("by default nothing is human-verified: authentic labels stay provisional, synthetic stay construction truth", () => {
  const dir = sandbox();
  const { records, summary } = loadAnnotations(dir);
  assert.equal(summary.human_verified, 0);
  assert.equal(summary.assistant_read_provisional_in_use, 2);
  assert.equal(records.filter((r) => r.annotation_status === "human_verified").length, 0);
});

test("a policy of 'excluded' withholds every assistant-read label from scoring but leaves synthetic labels alone", () => {
  const dir = sandbox();
  fs.writeFileSync(path.join(dir, "verification", "label_policy.json"), JSON.stringify({ assistant_read_labels: "excluded" }));
  const { records, summary } = loadAnnotations(dir);
  assert.equal(summary.mode, "verified_only");
  assert.equal(summary.assistant_read_excluded, 2);
  const a1 = records.find((r) => r.receipt_id === "a1");
  assert.ok(Object.values(a1.fields).every((f) => f.excluded));
  assert.ok(!records.find((r) => r.receipt_id === "s1").fields.pump_name.excluded);
  const pred = { pump_name: { normalized_value: "WRONG NAME", review_required: false }, transaction_date: { normalized_value: "2026-01-01", review_required: false }, total_amount: { normalized_value: 1, review_required: false }, overall_review_required: false };
  const scored = scoreReceipt(pred, a1);
  assert.equal(scored.unscored, true);
  assert.equal(scored.pump_name.falseConfident, false, "an excluded label can never be called a false-confident error");
  const agg = aggregate([scored]);
  assert.equal(agg.receipts, 0);
  assert.equal(agg.unscored_labels_excluded, 1);
});

test("an overlay entry without a reviewer id or without authorization is ignored, never trusted", () => {
  const dir = sandbox();
  fs.writeFileSync(path.join(dir, "corpus", "verified_labels.json"), JSON.stringify({ records: { a1: goodEntry({ authorization_confirmed: false }), a2: goodEntry({ reviewer_id: "  " }) } }));
  const { summary, records } = loadAnnotations(dir);
  assert.equal(summary.human_verified, 0);
  assert.equal(summary.ignored_invalid_overlay_entries, 2);
  assert.ok(records.every((r) => r.annotation_status !== "human_verified"));
  assert.equal(overlayEntryIsValid(goodEntry({ verified_at: undefined })), false);
});

test("a valid reviewer entry marks the label human_verified, keeps the provisional label and records agreement", () => {
  const dir = sandbox();
  fs.writeFileSync(path.join(dir, "corpus", "verified_labels.json"), JSON.stringify({ records: { a1: goodEntry({ total_amount: { status: "read", printed_text: "03523.45", value: 3523.45 } }) } }));
  const { summary, records } = loadAnnotations(dir);
  assert.equal(summary.human_verified, 1);
  const a1 = records.find((r) => r.receipt_id === "a1");
  assert.equal(a1.annotation_status, "human_verified");
  assert.equal(a1.fields.total_amount.normalized_value, 3523.45, "the human's value replaces the provisional one");
  assert.equal(a1.provisional_label.total_amount.normalized_value, 3528.45, "the provisional value is kept for audit");
  assert.deepEqual(a1.verification.agreement_with_provisional, { pump_name: true, transaction_date: true, total_amount: false });
  assert.equal(records.find((r) => r.receipt_id === "a2").annotation_status, "provisional_assistant_read");
});

test("a reviewer who finds a field illegible excludes that field instead of leaving a guess", () => {
  const out = applyReviewerEntry(rec("a1", "authentic", "provisional_assistant_read", "X FUELS", "2026-09-24", 100), goodEntry({ total_amount: { status: "illegible" } }));
  assert.equal(out.fields.total_amount.excluded, true);
  assert.equal(out.fields.pump_name.excluded, false);
});

test("apply_verification: rejects an export without authorization; verifies only when authorized", () => {
  const dir = sandbox();
  const mk = (name, body) => { const f = path.join(dir, name); fs.writeFileSync(f, JSON.stringify(body)); return f; };
  const rec1 = { pump_name: goodEntry().pump_name, transaction_date: goodEntry().transaction_date, total_amount: goodEntry().total_amount };
  const bad = mk("bad.json", { schema: "fuel-review-export/v1", reviewer_id: "REV-R1", authorization_confirmed: false, exported_at: "2026-10-21T10:00:00Z", records: { a1: rec1 } });
  assert.throws(() => execFileSync("node", [path.join(FUEL, "scripts", "apply_verification.mjs"), bad, "--annotations-dir", dir], { stdio: "pipe" }), /authorization_confirmed/);
  assert.ok(!fs.existsSync(path.join(dir, "corpus", "verified_labels.json")));
  const good = mk("good.json", { schema: "fuel-review-export/v1", reviewer_id: "REV-R1", authorization_confirmed: true, exported_at: "2026-10-21T10:00:00Z", records: { a1: rec1 } });
  execFileSync("node", [path.join(FUEL, "scripts", "apply_verification.mjs"), good, "--annotations-dir", dir], { stdio: "pipe" });
  const v = JSON.parse(fs.readFileSync(path.join(dir, "corpus", "verified_labels.json"), "utf8"));
  assert.deepEqual(Object.keys(v.records), ["a1"]);
  assert.equal(loadAnnotations(dir).summary.human_verified, 1);
});

test("apply_verification: two reviewers who disagree send the receipt to adjudication and it is NOT verified", () => {
  const dir = sandbox();
  const entry = (amount) => ({ pump_name: goodEntry().pump_name, transaction_date: goodEntry().transaction_date, total_amount: { status: "read", printed_text: String(amount), value: amount } });
  const f1 = path.join(dir, "r1.json"), f2 = path.join(dir, "r2.json");
  fs.writeFileSync(f1, JSON.stringify({ schema: "fuel-review-export/v1", reviewer_id: "REV-R1", authorization_confirmed: true, exported_at: "2026-10-21T10:00:00Z", records: { a1: entry(3528.45), a2: { ...entry(4762.93), pump_name: { status: "read", value: "ONKAR SERVICE" }, transaction_date: { status: "read", iso_date: "2026-08-25", order_provable_from_receipt: true } } } }));
  fs.writeFileSync(f2, JSON.stringify({ schema: "fuel-review-export/v1", reviewer_id: "REV-R2", authorization_confirmed: true, exported_at: "2026-10-21T11:00:00Z", records: { a1: entry(3523.45), a2: { ...entry(4762.93), pump_name: { status: "read", value: "ONKAR SERVICE" }, transaction_date: { status: "read", iso_date: "2026-08-25", order_provable_from_receipt: true } } } }));
  execFileSync("node", [path.join(FUEL, "scripts", "apply_verification.mjs"), f1, f2, "--annotations-dir", dir], { stdio: "pipe" });
  const v = JSON.parse(fs.readFileSync(path.join(dir, "corpus", "verified_labels.json"), "utf8"));
  assert.deepEqual(Object.keys(v.records), ["a2"], "only the receipt both reviewers agree on is verified");
  assert.ok(JSON.parse(fs.readFileSync(path.join(dir, "corpus", "adjudication_needed.json"), "utf8")).a1);
});

test("review pack: offline (no network references), blind by default", () => {
  const out = fs.mkdtempSync(path.join(os.tmpdir(), "fuel-pack-"));
  execFileSync("node", [path.join(FUEL, "scripts", "build_review_pack.mjs"), "--out", out], { stdio: "pipe" });
  const html = fs.readFileSync(path.join(out, "review.html"), "utf8");
  assert.ok(!/(src|href)\s*=\s*["']https?:/i.test(html) && !/@import|fetch\(|XMLHttpRequest|WebSocket/.test(html), "the review page must make no network request");
  const m = fs.readFileSync(path.join(out, "manifest.js"), "utf8");
  const manifest = JSON.parse(m.replace(/^window\.REVIEW_MANIFEST = /, "").replace(/;\s*$/, ""));
  assert.equal(manifest.blind, true);
  assert.ok(manifest.items.length >= 22);
  assert.ok(manifest.items.every((it) => it.provisional === null), "blind mode shows no provisional label");
  assert.ok(fs.existsSync(path.join(out, "review_sheet.csv")) && fs.existsSync(path.join(out, "INSTRUCTIONS.txt")));
});

test("real corpus guard: every human_verified label has a reviewer id and an authorization confirmation (currently none exist)", () => {
  const { records, summary } = loadAnnotations(FUEL, { labels: "all" });
  for (const r of records.filter((x) => x.annotation_status === "human_verified")) {
    assert.ok(r.verification.reviewer_id && r.verification.authorization_confirmed === true, r.receipt_id);
  }
  assert.equal(records.filter((x) => x.annotation_status === "human_verified").length, summary.human_verified);
});
