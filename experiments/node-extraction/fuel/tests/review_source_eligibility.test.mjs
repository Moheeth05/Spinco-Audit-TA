// Source-type eligibility for human verification. Every test runs the COPIED scripts inside a synthetic mirror tree (tests/helpers/syntheticMirror.mjs):
// invented fixtures only; the real corpus, labels, review pack and policy are never read or written.
import test from "node:test";
import assert from "node:assert/strict";
import crypto from "node:crypto";
import fs from "node:fs";
import path from "node:path";
import { pathToFileURL } from "node:url";
import { makeMirror, assertIsolated, applyExports, exportFile, goodEntry, readJson, rec, placeholderSha256 } from "./helpers/syntheticMirror.mjs";

const sha = (f) => crypto.createHash("sha256").update(fs.readFileSync(f)).digest("hex");
const mirrorLabels = async (root) => (await import(pathToFileURL(path.join(root, "src", "labels.mjs")).href + `?t=${Math.random()}`));
const byId = (records, id) => records.find((r) => r.receipt_id === id);
const noVerified = (m) => assert.ok(!fs.existsSync(m.verifiedFile), "nothing may be written as verified");

test("mirror used by these tests is provably isolated and synthetic", () => {
  const m = makeMirror();
  assert.equal(assertIsolated(m.root), true);
});

test("B1: a valid review of a SYNTHETIC record is rejected at the application boundary and its construction-truth label is preserved", async () => {
  const m = makeMirror();
  const before = sha(path.join(m.root, "corpus", "annotations.json"));
  const r = applyExports(m.root, [exportFile("REV-001", { "SYNTH-FUEL-001": goodEntry() })]);
  assert.equal(r.status, 1, r.all);
  assert.match(r.all, /SYNTH-FUEL-001/);
  assert.match(r.all, /not eligible/);
  noVerified(m);
  assert.equal(sha(path.join(m.root, "corpus", "annotations.json")), before);
  const { records, summary } = (await mirrorLabels(m.root)).loadAnnotations(m.root);
  const s = byId(records, "SYNTH-FUEL-001");
  assert.equal(s.annotation_status, "construction_truth");
  assert.equal(s.fields.pump_name.normalized_value, "SYNTHETIC PUMP");
  assert.equal(summary.human_verified, 0);
});

test("B2: a CONTROL record can never become human-verified", () => {
  const m = makeMirror();
  const r = applyExports(m.root, [exportFile("REV-001", { "SYNTH-CTRL-001": goodEntry() })]);
  assert.equal(r.status, 1, r.all);
  assert.match(r.all, /generic_control/);
  noVerified(m);
});

test("B3: an unknown, mistyped or MISSING source type fails closed", () => {
  const extra = [rec("SYNTH-WEIRD-001", "mystery_type", "provisional_assistant_read", "X", "2026-01-02", 10), rec("SYNTH-NOTYPE-001", undefined, "provisional_assistant_read", "X", "2026-01-02", 10), rec("SYNTH-CASE-001", "Authentic", "provisional_assistant_read", "X", "2026-01-02", 10)];
  for (const id of ["SYNTH-WEIRD-001", "SYNTH-NOTYPE-001", "SYNTH-CASE-001"]) {
    const m = makeMirror({ extraRecords: extra });
    const r = applyExports(m.root, [exportFile("REV-001", { [id]: goodEntry() })]);
    assert.equal(r.status, 1, `${id}: ${r.all}`);
    assert.match(r.all, new RegExp(id));
    noVerified(m);
  }
});

test("B4: eligibility comes from the corpus source type, never from the id's spelling (both directions)", () => {
  const extra = [
    rec("SYNTH-LOOKS-LIKE-SYNTHETIC", "authentic", "provisional_assistant_read", "SYNTHETIC DEALER ONE", "2026-09-24", 100), // id says synthetic, type is authentic -> eligible
    rec("SYNTH-AUTHENTIC-LOOKING", "synthetic", "construction_truth", "SYNTHETIC DEALER ONE", "2026-09-24", 100),               // id says authentic, type is synthetic -> blocked
  ];
  const ok = makeMirror({ extraRecords: extra });
  assert.equal(applyExports(ok.root, [exportFile("REV-001", { "SYNTH-LOOKS-LIKE-SYNTHETIC": goodEntry() })]).status, 0);
  assert.deepEqual(Object.keys(readJson(ok.verifiedFile).records), ["SYNTH-LOOKS-LIKE-SYNTHETIC"]);
  const blocked = makeMirror({ extraRecords: extra });
  const r = applyExports(blocked.root, [exportFile("REV-001", { "SYNTH-AUTHENTIC-LOOKING": goodEntry() })]);
  assert.equal(r.status, 1, r.all);
  noVerified(blocked);
});

test("B5: a user-supplied source type inside the export is ignored; only the corpus decides", () => {
  const m = makeMirror();
  const e = exportFile("REV-001", { "SYNTH-FUEL-001": { ...goodEntry(), source_type: "authentic" } }, { source_type: "authentic" });
  assert.equal(applyExports(m.root, [e]).status, 1);
  noVerified(m);
});

test("B6: eligible authentic and suspected-template records still verify, and the existing validation rules still apply to them", () => {
  const m = makeMirror();
  const tpl = { ...goodEntry(), pump_name: { status: "read", value: "SYNTHETIC TEMPLATE DEALER" }, total_amount: { status: "read", value: 300 }, transaction_date: { status: "read", iso_date: "2026-07-15", order_provable_from_receipt: false, order_evidence: "none" } };
  const r = applyExports(m.root, [exportFile("REV-001", { "SYNTH-AUTH-001": goodEntry(), "SYNTH-TPL-001": tpl })]);
  assert.equal(r.status, 0, r.all);
  assert.deepEqual(Object.keys(readJson(m.verifiedFile).records).sort(), ["SYNTH-AUTH-001", "SYNTH-TPL-001"]);
  const bad = makeMirror();
  const r2 = applyExports(bad.root, [exportFile("REV-001", { "SYNTH-AUTH-001": { ...goodEntry(), transaction_date: { status: "read", iso_date: "2026-02-30", order_provable_from_receipt: true, order_evidence: "month_name_or_iso" } } })]);
  assert.equal(r2.status, 1);
  assert.match(r2.all, /transaction_date/);
  noVerified(bad);
});

test("B7: one ineligible receipt in an otherwise valid export rejects the whole invocation (no partial application, no history written)", () => {
  const m = makeMirror();
  const r = applyExports(m.root, [exportFile("REV-001", { "SYNTH-AUTH-001": goodEntry(), "SYNTH-FUEL-001": goodEntry() })]);
  assert.equal(r.status, 1, r.all);
  noVerified(m);
  assert.ok(!fs.existsSync(path.join(m.root, "corpus", "review_history.json")));
});

test("B8: a hand-written overlay for a synthetic, control or unknown-type record is ignored by the loader (and counted); an eligible one still loads", async () => {
  const extra = [rec("SYNTH-WEIRD-001", "mystery_type", "provisional_assistant_read", "X", "2026-01-02", 10)];
  const m = makeMirror({ extraRecords: extra });
  const entry = (o = {}) => ({ reviewer_id: "REV-001", authorization_confirmed: true, verified_at: "2026-10-22T10:00:00Z", ...goodEntry(), ...o });
  fs.writeFileSync(m.verifiedFile, JSON.stringify({ records: { "SYNTH-FUEL-001": entry(), "SYNTH-CTRL-001": entry(), "SYNTH-WEIRD-001": entry(), "SYNTH-AUTH-001": entry() } }));
  const { records, summary } = (await mirrorLabels(m.root)).loadAnnotations(m.root);
  assert.equal(summary.human_verified, 1);
  assert.equal(summary.ignored_ineligible_overlay_entries, 3);
  assert.equal(byId(records, "SYNTH-AUTH-001").annotation_status, "human_verified");
  assert.equal(byId(records, "SYNTH-FUEL-001").annotation_status, "construction_truth");
  assert.equal(byId(records, "SYNTH-FUEL-001").fields.pump_name.normalized_value, "SYNTHETIC PUMP");
  assert.equal(byId(records, "SYNTH-CTRL-001").annotation_status, "construction_truth");
  assert.equal(byId(records, "SYNTH-WEIRD-001").annotation_status, "provisional_assistant_read");
});

test("B9: an invalid overlay for an ELIGIBLE record stays blocked (existing rules unchanged)", async () => {
  const m = makeMirror();
  const e = { reviewer_id: "REV-001", authorization_confirmed: true, verified_at: "2026-10-22T10:00:00Z", ...goodEntry({ pump_name: { status: "banana", value: "X" } }) };
  fs.writeFileSync(m.verifiedFile, JSON.stringify({ records: { "SYNTH-AUTH-001": e } }));
  const { summary } = (await mirrorLabels(m.root)).loadAnnotations(m.root);
  assert.equal(summary.human_verified, 0);
  assert.equal(summary.ignored_invalid_overlay_entries, 1);
});

test("B10: applyReviewerEntry itself refuses a synthetic / control / unknown-type record", async () => {
  const { applyReviewerEntry } = await mirrorLabels(makeMirror().root);
  const e = { reviewer_id: "REV-001", authorization_confirmed: true, verified_at: "2026-10-22T10:00:00Z", ...goodEntry() };
  for (const t of ["synthetic", "generic_control", "mystery", undefined]) assert.throws(() => applyReviewerEntry(rec("SYNTH-X", t, "construction_truth", "P", "2026-01-02", 1), e), /not eligible/);
  assert.doesNotThrow(() => applyReviewerEntry(rec("SYNTH-X", "authentic", "provisional_assistant_read", "P", "2026-01-02", 1), e));
  assert.doesNotThrow(() => applyReviewerEntry(rec("SYNTH-X", "suspected_template_generated", "provisional_assistant_read", "P", "2026-01-02", 1), e));
});

test("B11: no existing verified authentic record is silently overwritten (a conflict is still recorded, even in a mixed run)", async () => {
  const m = makeMirror();
  assert.equal(applyExports(m.root, [exportFile("REV-R1", { "SYNTH-AUTH-001": goodEntry() })]).status, 0);
  const before = sha(m.verifiedFile);
  const later = { ...goodEntry(), total_amount: { status: "read", value: 999 } };
  const r = applyExports(m.root, [exportFile("REV-R2", { "SYNTH-AUTH-001": later })]);
  assert.equal(r.status, 3, r.all);
  assert.equal(sha(m.verifiedFile), before);
  const mixed = applyExports(m.root, [exportFile("REV-R3", { "SYNTH-AUTH-001": later, "SYNTH-CTRL-001": goodEntry() })]);
  assert.equal(mixed.status, 1, "an ineligible id rejects the run before any conflict handling");
  assert.equal(sha(m.verifiedFile), before);
  const { summary } = (await mirrorLabels(m.root)).loadAnnotations(m.root);
  assert.equal(summary.held_by_open_conflict, 1);
  assert.equal(summary.human_verified, 0);
});

test("B12: intake receipts - only an entry with authentic_issued_receipt provenance is eligible", () => {
  const reg = { receipts: [
    { receipt_id: "SYNTH-INTAKE-OK", image_path: "corpus/authentic_intake/pending/SYNTH-INTAKE-OK.png", sha256: placeholderSha256("SYNTH-INTAKE-OK"), split: "development", receipt_family: "fam_a", brand_family: "other", label_status: "pending_human_label", provenance: { source_type: "authentic_issued_receipt", source_description: "synthetic fixture" } },
    { receipt_id: "SYNTH-INTAKE-BAD", image_path: "x.png", split: "development", receipt_family: "fam_b", brand_family: "other", label_status: "pending_human_label", provenance: { source_type: "synthetic_template", source_description: "synthetic fixture" } },
    { receipt_id: "SYNTH-INTAKE-NOPROV", image_path: "y.png", split: "development", receipt_family: "fam_c", brand_family: "other", label_status: "pending_human_label" },
  ] };
  const m = makeMirror({ intake: reg });
  assert.equal(assertIsolated(m.root), true);
  assert.equal(applyExports(m.root, [exportFile("REV-001", { "SYNTH-INTAKE-OK": goodEntry() })]).status, 0);
  for (const id of ["SYNTH-INTAKE-BAD", "SYNTH-INTAKE-NOPROV"]) {
    const r = applyExports(m.root, [exportFile("REV-001", { [id]: goodEntry() })]);
    assert.equal(r.status, 1, `${id}: ${r.all}`);
    assert.match(r.all, /provenance/);
  }
  assert.deepEqual(Object.keys(readJson(m.verifiedFile).records), ["SYNTH-INTAKE-OK"]);
});

test("B13: an id that is not in the corpus at all is still warned about and ignored (existing behaviour unchanged)", () => {
  const m = makeMirror();
  const r = applyExports(m.root, [exportFile("REV-001", { "SYNTH-AUTH-001": goodEntry(), "SYNTH-NOT-IN-CORPUS": goodEntry() })]);
  assert.equal(r.status, 0, r.all);
  assert.match(r.err, /ignored unknown receipt id SYNTH-NOT-IN-CORPUS/);
  assert.deepEqual(Object.keys(readJson(m.verifiedFile).records), ["SYNTH-AUTH-001"]);
});
