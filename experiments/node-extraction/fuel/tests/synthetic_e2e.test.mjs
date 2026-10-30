// Synthetic-only END-TO-END run of the review workflow, executed from a MIRROR tree (tests/helpers/syntheticMirror.mjs):
// build pack -> valid export -> invalid export -> duplicate reviewer -> conflicting later review -> ineligible record -> reload labels.
// The copied scripts derive their data directory from their own location, so every read and write happens inside the mirror.
// Mirrors go to the OS temp dir, or inside FUEL_SYNTHETIC_E2E_ROOT if set (e.g. a kept folder under the fuel experiment). Nothing is deleted.
import test from "node:test";
import assert from "node:assert/strict";
import fs from "node:fs";
import path from "node:path";
import { pathToFileURL } from "node:url";
import { makeMirror, assertIsolated, runScript, applyExports, goodEntry, exportFile, readJson } from "./helpers/syntheticMirror.mjs";
import * as RV from "../src/reviewValidation.mjs";

const m = makeMirror();
const labels = await import(pathToFileURL(path.join(m.root, "src", "labels.mjs")).href);
const load = () => labels.loadAnnotations(m.root);
const byId = (records, id) => records.find((r) => r.receipt_id === id);
const entry2 = () => ({ ...goodEntry(), pump_name: { status: "read", value: "SYNTHETIC DEALER TWO" }, transaction_date: { status: "read", iso_date: "2026-08-25", order_provable_from_receipt: true, order_evidence: "component_gt_12" }, total_amount: { status: "read", value: 200 } });
const items = [{ id: "SYNTH-AUTH-001" }, { id: "SYNTH-AUTH-002" }, { id: "SYNTH-TPL-001" }];
const ui = (state, reviewerId = "REV-E2E-1") => RV.buildExportPayload({ items, state, reviewerId, authorizationConfirmed: true, packId: "synthetic-e2e", blind: true, exportedAt: "2026-10-22T10:00:00Z" });
let sharedPayload;

test("E2E 0: every path the run will use resolves inside the synthetic mirror (checked BEFORE any script runs)", () => {
  assert.equal(assertIsolated(m.root), true);
  assert.ok(!fs.existsSync(m.verifiedFile));
  assert.deepEqual(load().records.map((r) => r.receipt_id).sort(), ["SYNTH-AUTH-001", "SYNTH-AUTH-002", "SYNTH-CTRL-001", "SYNTH-FUEL-001", "SYNTH-TPL-001"]);
});

test("E2E 1: the review pack is generated from the mirror, blind, offering only verifiable synthetic fixtures", () => {
  const r = runScript(m.root, "build_review_pack.mjs", ["--out", m.packDir]);
  assert.equal(r.status, 0, r.all);
  assert.match(r.out, new RegExp(m.packDir.replace(/[\\^$.*+?()[\]{}|]/g, "\\$&")));
  const manifest = JSON.parse(fs.readFileSync(path.join(m.packDir, "manifest.js"), "utf8").replace(/^window\.REVIEW_MANIFEST = /, "").replace(/;\s*$/, ""));
  assert.equal(manifest.blind, true);
  assert.deepEqual(manifest.items.map((x) => x.id).sort(), ["SYNTH-AUTH-001", "SYNTH-AUTH-002", "SYNTH-TPL-001"]);
  assert.ok(manifest.items.every((x) => x.id.startsWith("SYNTH-") && x.provisional === null));
});

test("E2E 2: a valid review exports through the UI rules and verifies the eligible fixtures", () => {
  const tpl = { ...goodEntry(), pump_name: { status: "read", value: "SYNTHETIC TEMPLATE DEALER" }, transaction_date: { status: "read", iso_date: "2026-07-15", order_provable_from_receipt: true, order_evidence: "component_gt_12" }, total_amount: { status: "read", value: 300 } };
  const res = ui({ "SYNTH-AUTH-001": goodEntry(), "SYNTH-AUTH-002": entry2(), "SYNTH-TPL-001": tpl });
  assert.equal(res.ok, true, JSON.stringify(res.errors));
  sharedPayload = res.payload;
  const r = applyExports(m.root, [res.payload]);
  assert.equal(r.status, 0, r.all);
  const { records, summary } = load();
  assert.equal(summary.human_verified, 3);
  for (const id of ["SYNTH-AUTH-001", "SYNTH-AUTH-002", "SYNTH-TPL-001"]) assert.equal(byId(records, id).annotation_status, "human_verified");
  assert.equal(byId(records, "SYNTH-FUEL-001").annotation_status, "construction_truth");
  assert.equal(byId(records, "SYNTH-CTRL-001").annotation_status, "construction_truth");
});

test("E2E 3: an invalid review cannot be exported by the UI, and the same content is rejected by apply_verification (verified file unchanged)", () => {
  const before = fs.readFileSync(m.verifiedFile, "utf8");
  const stale = { ...goodEntry(), pump_name: { status: "illegible", value: "STALE VALUE" } };
  assert.equal(ui({ "SYNTH-AUTH-001": stale }).ok, false);
  const r = applyExports(m.root, [exportFile("REV-E2E-9", { "SYNTH-AUTH-001": stale })]);
  assert.equal(r.status, 1, r.all);
  assert.match(r.all, /contradicts/);
  assert.equal(fs.readFileSync(m.verifiedFile, "utf8"), before);
});

test("E2E 4: a duplicate reviewer is rejected and is never counted as an independent second reviewer", () => {
  const before = fs.readFileSync(m.verifiedFile, "utf8");
  const r = applyExports(m.root, [exportFile("REV-E2E-5", { "SYNTH-AUTH-001": goodEntry() }), exportFile("rev-e2e-5", { "SYNTH-AUTH-001": goodEntry() })]);
  assert.equal(r.status, 1, r.all);
  assert.match(r.all, /duplicate reviewer identity/);
  assert.equal(fs.readFileSync(m.verifiedFile, "utf8"), before);
});

test("E2E 5: a conflicting later review is recorded as an open conflict and does not replace the earlier label", () => {
  const before = fs.readFileSync(m.verifiedFile, "utf8");
  const r = applyExports(m.root, [exportFile("REV-E2E-2", { "SYNTH-AUTH-001": { ...goodEntry(), total_amount: { status: "read", value: 999 } } })]);
  assert.equal(r.status, 3, r.all);
  assert.match(r.all, /CONFLICT/);
  assert.equal(fs.readFileSync(m.verifiedFile, "utf8"), before, "verified_labels.json is byte-identical");
  const cf = readJson(path.join(m.root, "corpus", "review_conflicts.json"));
  assert.equal(cf.events.length, 1);
  assert.equal(cf.events[0].status, "open");
  assert.deepEqual(cf.events[0].fields_in_conflict, ["total_amount"]);
});

test("E2E 6: a review that names a synthetic or control record is rejected outright", () => {
  const before = fs.readFileSync(m.verifiedFile, "utf8");
  for (const id of ["SYNTH-FUEL-001", "SYNTH-CTRL-001"]) {
    const r = applyExports(m.root, [exportFile("REV-E2E-3", { [id]: goodEntry() })]);
    assert.equal(r.status, 1, r.all);
    assert.match(r.all, /not eligible/);
  }
  assert.equal(fs.readFileSync(m.verifiedFile, "utf8"), before);
});

test("E2E 7: reloading the labels - disputed and disallowed records are not human-verified; the rest are", () => {
  const { records, summary } = load();
  assert.equal(byId(records, "SYNTH-AUTH-001").annotation_status, "provisional_assistant_read", "disputed -> held back");
  assert.equal(byId(records, "SYNTH-AUTH-002").annotation_status, "human_verified");
  assert.equal(byId(records, "SYNTH-TPL-001").annotation_status, "human_verified");
  assert.equal(byId(records, "SYNTH-FUEL-001").annotation_status, "construction_truth");
  assert.equal(byId(records, "SYNTH-CTRL-001").annotation_status, "construction_truth");
  assert.equal(summary.human_verified, 2);
  assert.equal(summary.held_by_open_conflict, 1);
  const history = readJson(path.join(m.root, "corpus", "review_history.json"));
  assert.equal(history.events.length, 2, "only the two invocations that passed validation are in the audit history (valid + conflicting)");
  assert.equal(sharedPayload.reviewer_id, "REV-E2E-1");
});

test("E2E 8: the generated artifacts all live inside the mirror, which holds nothing but synthetic ids", () => {
  const walk = (d) => fs.readdirSync(d, { withFileTypes: true }).flatMap((e) => (e.isDirectory() ? walk(path.join(d, e.name)) : [path.join(d, e.name)]));
  const files = walk(m.root);
  assert.ok(files.length > 10);
  for (const f of files) assert.ok(path.resolve(f).startsWith(path.resolve(m.root) + path.sep));
  const text = ["verified_labels.json", "review_conflicts.json", "review_history.json"].map((n) => fs.readFileSync(path.join(m.root, "corpus", n), "utf8")).join("\n");
  const ids = [...text.matchAll(/"receipt_id":\s*"([^"]+)"/g)].map((x) => x[1]);
  assert.ok(ids.every((x) => x.startsWith("SYNTH-")));
  assert.equal(assertIsolated(m.root), true);
  console.log(`# synthetic mirror kept at: ${m.root}`);
});
