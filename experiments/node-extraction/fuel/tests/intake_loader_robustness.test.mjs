// Robustness of the label loader (src/labels.mjs) against malformed intake-register entries, especially a missing / null / malformed `provenance`.
// Fail-closed contract under test: an intake entry is loaded (as a PENDING, fully-excluded record) only if the register itself proves it is an
// authentic issued receipt (provenance.source_type === "authentic_issued_receipt"); anything else is SKIPPED and reported - never crashed on, never
// scored, never run, never human-verified - and eligibility is never inferred from the receipt id.
// Synthetic only: every test builds a mirror tree (tests/helpers/syntheticMirror.mjs) with invented ids; the real corpus/intake are never read.
import test from "node:test";
import assert from "node:assert/strict";
import fs from "node:fs";
import path from "node:path";
import { pathToFileURL } from "node:url";
import { makeMirror, assertIsolated, applyExports, exportFile, goodEntry, readJson, placeIntakeImages, placeholderSha256 } from "./helpers/syntheticMirror.mjs";

const validEntry = (id, o = {}) => ({
  receipt_id: id, image_path: `corpus/authentic_intake/pending/${id}.png`, sha256: placeholderSha256(id), registered_at: "2026-10-22T00:00:00Z", split: "development",
  receipt_family: "synthetic_family_a", brand_family: "other", label_status: "pending_human_label",
  provenance: { source_type: "authentic_issued_receipt", source_description: "synthetic fixture description" }, ...o,
});
const { provenance: _omit, ...noProv } = validEntry("x"); // eslint-disable-line no-unused-vars
const inside = (root, p) => { const rel = path.relative(root, path.resolve(p)); return rel === "" || (!rel.startsWith("..") && !path.isAbsolute(rel)); };

/** A fresh synthetic mirror whose intake register holds `receipts` (written AFTER the isolation check, always inside the mirror). */
function mirrorWithRegister(receipts) {
  const m = makeMirror();
  assertIsolated(m.root);
  const reg = path.join(m.root, "intake", "intake_register.json");
  assert.ok(inside(m.root, reg), "register path is inside the mirror");
  for (const r of receipts) if (r && typeof r.receipt_id === "string") assert.ok(r.receipt_id.startsWith("SYNTH-"), "fixture ids are synthetic");
  fs.writeFileSync(reg, JSON.stringify({ note: "SYNTHETIC TEST FIXTURE", receipts }));
  placeIntakeImages(m.root, { receipts }); // a registered receipt has its (blank, non-empty) placeholder image; the applier requires an available image
  return m;
}
const mirrorLabels = (root) => import(pathToFileURL(path.join(root, "src", "labels.mjs")).href + `?t=${Math.random()}`);
async function load(m) {
  const L = await mirrorLabels(m.root);
  const warnings = [], orig = console.warn;
  console.warn = (...a) => warnings.push(a.join(" "));
  try { return { ...L.loadAnnotations(m.root), warnings, L }; } finally { console.warn = orig; }
}
const ids = (records) => records.map((r) => r.receipt_id);

test("missing provenance (key absent): the loader does not crash, skips the entry, reports it, and still loads valid neighbours", async () => {
  const m = mirrorWithRegister([{ ...noProv, receipt_id: "SYNTH-INTAKE-NOPROV" }, validEntry("SYNTH-INTAKE-OK")]);
  const { records, summary, warnings } = await load(m);
  assert.ok(!ids(records).includes("SYNTH-INTAKE-NOPROV"), "an entry without provenance is not loaded as a record");
  assert.ok(ids(records).includes("SYNTH-INTAKE-OK"));
  assert.equal(summary.skipped_intake_entries, 1);
  assert.equal(summary.skipped_intake_detail[0].receipt_id, "SYNTH-INTAKE-NOPROV");
  assert.match(summary.skipped_intake_detail[0].reason, /provenance/);
  assert.ok(warnings.some((w) => w.includes("SYNTH-INTAKE-NOPROV")), "the skip is announced");
  assert.equal(summary.human_verified, 0);
});

test("null provenance: no crash, entry skipped and reported", async () => {
  const m = mirrorWithRegister([validEntry("SYNTH-INTAKE-NULL", { provenance: null })]);
  const { records, summary } = await load(m);
  assert.ok(!ids(records).includes("SYNTH-INTAKE-NULL"));
  assert.equal(summary.skipped_intake_entries, 1);
});

test("malformed provenance (wrong type, empty, or not proving an authentic issued receipt): never loaded, never crashes", async () => {
  const bad = ["not an object", 5, true, [], [{ source_type: "authentic_issued_receipt" }], {}, { source_description: "no type" }, { source_type: "synthetic_template", source_description: "generator output" }, { source_type: "AUTHENTIC_ISSUED_RECEIPT", source_description: "wrong case" }, { source_type: ["authentic_issued_receipt"], source_description: "array type" }];
  for (const [i, prov] of bad.entries()) {
    const id = `SYNTH-INTAKE-BAD-${i}`;
    const m = mirrorWithRegister([validEntry(id, { provenance: prov }), validEntry("SYNTH-INTAKE-OK")]);
    const { records, summary } = await load(m);
    assert.ok(!ids(records).includes(id), `provenance ${JSON.stringify(prov)} must not load`);
    assert.ok(ids(records).includes("SYNTH-INTAKE-OK"));
    assert.equal(summary.skipped_intake_entries, 1, JSON.stringify(prov));
    assert.equal(summary.human_verified, 0);
  }
});

test("valid provenance: loads exactly as before (pending, every field excluded, mapped split, description kept) with no warning", async () => {
  const m = mirrorWithRegister([validEntry("SYNTH-INTAKE-DEV"), validEntry("SYNTH-INTAKE-HELD", { split: "heldout_untouched", receipt_family: "synthetic_family_b", brand_family: "Nayara" })]);
  const { records, summary, warnings } = await load(m);
  const dev = records.find((r) => r.receipt_id === "SYNTH-INTAKE-DEV"), held = records.find((r) => r.receipt_id === "SYNTH-INTAKE-HELD");
  assert.equal(dev.split, "intake_dev");
  assert.equal(held.split, "intake_heldout");
  assert.equal(held.brand_family, "Nayara");
  assert.equal(dev.source_type, "authentic");
  assert.equal(dev.source_reference, "intake: synthetic fixture description");
  assert.equal(dev.image_path, "corpus/authentic_intake/pending/SYNTH-INTAKE-DEV.png");
  assert.equal(dev.annotation_status, "pending_human_label");
  assert.equal(dev.manually_reviewed_by_independent_human, false);
  assert.ok(Object.values(dev.fields).every((f) => f.excluded === true && f.normalized_value === null));
  assert.equal(summary.skipped_intake_entries, 0);
  assert.deepEqual(summary.skipped_intake_detail, []);
  assert.deepEqual(warnings, []);
});

test("valid authentic source type but a missing / non-text description: still loads (it is eligible), with a neutral description", async () => {
  for (const [i, prov] of [{ source_type: "authentic_issued_receipt" }, { source_type: "authentic_issued_receipt", source_description: 123 }, { source_type: "authentic_issued_receipt", source_description: { a: 1 } }].entries()) {
    const id = `SYNTH-INTAKE-NODESC-${i}`;
    const m = mirrorWithRegister([validEntry(id, { provenance: prov })]);
    const { records, summary } = await load(m);
    const r = records.find((x) => x.receipt_id === id);
    assert.ok(r, "an entry that proves it is authentic still loads");
    assert.equal(r.annotation_status, "pending_human_label");
    assert.match(r.source_reference, /^intake: \(no description recorded\)$/);
    assert.equal(summary.skipped_intake_entries, 0);
  }
});

test("eligibility is never inferred from the receipt id: an authentic-looking id without provenance is skipped; a synthetic-looking id with provenance loads", async () => {
  const m = mirrorWithRegister([{ ...noProv, receipt_id: "SYNTH-AUTHENTIC-LOOKING-NOPROV" }, validEntry("SYNTH-SYNTHETIC-LOOKING-VALID")]);
  const { records, L } = await load(m);
  assert.ok(!ids(records).includes("SYNTH-AUTHENTIC-LOOKING-NOPROV"));
  assert.ok(ids(records).includes("SYNTH-SYNTHETIC-LOOKING-VALID"));
  const e = L.verificationEligibility([], readJson(path.join(m.root, "intake", "intake_register.json")).receipts);
  assert.ok(e.blocked.has("SYNTH-AUTHENTIC-LOOKING-NOPROV") && !e.eligible.has("SYNTH-AUTHENTIC-LOOKING-NOPROV"));
  assert.ok(e.eligible.has("SYNTH-SYNTHETIC-LOOKING-VALID") && !e.blocked.has("SYNTH-SYNTHETIC-LOOKING-VALID"));
});

test("a (valid-looking) human overlay for a skipped intake entry cannot create a record or a human_verified label", async () => {
  const m = mirrorWithRegister([{ ...noProv, receipt_id: "SYNTH-INTAKE-NOPROV" }, validEntry("SYNTH-INTAKE-OK")]);
  const e = { reviewer_id: "REV-001", authorization_confirmed: true, verified_at: "2026-10-22T10:00:00Z", ...goodEntry() };
  fs.writeFileSync(m.verifiedFile, JSON.stringify({ records: { "SYNTH-INTAKE-NOPROV": e } }));
  const { records, summary } = await load(m);
  assert.ok(!ids(records).includes("SYNTH-INTAKE-NOPROV"));
  assert.equal(summary.human_verified, 0);
  assert.equal(records.filter((r) => r.annotation_status === "human_verified").length, 0);
  fs.writeFileSync(m.verifiedFile, JSON.stringify({ records: { "SYNTH-INTAKE-OK": e } }));
  const after = await load(m);
  assert.equal(after.summary.human_verified, 1, "the entry that proves it is authentic can still be verified through a valid overlay");
});

test("apply_verification: a review naming an intake entry with missing / null / malformed provenance is rejected cleanly (no crash); a valid one verifies", () => {
  const m = mirrorWithRegister([{ ...noProv, receipt_id: "SYNTH-INTAKE-NOPROV" }, validEntry("SYNTH-INTAKE-NULL", { provenance: null }), validEntry("SYNTH-INTAKE-STR", { provenance: "x" }), validEntry("SYNTH-INTAKE-OK")]);
  for (const id of ["SYNTH-INTAKE-NOPROV", "SYNTH-INTAKE-NULL", "SYNTH-INTAKE-STR"]) {
    const r = applyExports(m.root, [exportFile("REV-001", { [id]: goodEntry() })]);
    assert.equal(r.status, 1, `${id}: ${r.all}`);
    assert.match(r.all, /provenance/);
    assert.doesNotMatch(r.all, /TypeError|at .*\.mjs:\d+/);
  }
  assert.ok(!fs.existsSync(m.verifiedFile));
  const ok = applyExports(m.root, [exportFile("REV-001", { "SYNTH-INTAKE-OK": goodEntry() })]);
  assert.equal(ok.status, 0, ok.all);
  assert.deepEqual(Object.keys(readJson(m.verifiedFile).records), ["SYNTH-INTAKE-OK"]);
});

test("non-object register entries and entries without a text receipt_id are skipped without crashing; valid entries still load", async () => {
  const m = mirrorWithRegister([null, "a string", 7, [], {}, validEntry("x", { receipt_id: undefined }), validEntry("x", { receipt_id: 42 }), validEntry("SYNTH-INTAKE-OK")]);
  const { records, summary, L } = await load(m);
  assert.deepEqual(ids(records).filter((id) => String(id).startsWith("SYNTH-INTAKE")), ["SYNTH-INTAKE-OK"]);
  assert.equal(summary.skipped_intake_entries, 7);
  assert.doesNotThrow(() => L.verificationEligibility([], readJson(path.join(m.root, "intake", "intake_register.json")).receipts));
  const e = L.verificationEligibility([], readJson(path.join(m.root, "intake", "intake_register.json")).receipts);
  assert.deepEqual([...e.eligible], ["SYNTH-INTAKE-OK"]);
});

test("an intake entry cannot override a blocked annotation id: a collision with a synthetic record stays ineligible and is skipped", async () => {
  const m = mirrorWithRegister([validEntry("SYNTH-FUEL-001")]); // SYNTH-FUEL-001 is a synthetic (construction-truth) record in the mirror annotations
  const { records, L, summary } = await load(m);
  assert.equal(records.filter((r) => r.receipt_id === "SYNTH-FUEL-001").length, 1, "only the original synthetic record exists");
  assert.equal(records.find((r) => r.receipt_id === "SYNTH-FUEL-001").annotation_status, "construction_truth");
  assert.equal(summary.skipped_intake_entries, 1);
  assert.ok(!L.verificationEligibility(JSON.parse(fs.readFileSync(path.join(m.root, "corpus", "annotations.json"), "utf8")).records, readJson(path.join(m.root, "intake", "intake_register.json")).receipts).eligible.has("SYNTH-FUEL-001"));
});
