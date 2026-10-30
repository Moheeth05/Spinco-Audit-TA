// Intake / held-out reservation tests. All inputs are throw-away dummy images in a temp directory (NOT receipts); the real corpus is never touched.
import test from "node:test";
import assert from "node:assert/strict";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import { execFileSync } from "node:child_process";
import { fileURLToPath } from "node:url";
import { loadAnnotations } from "../src/labels.mjs";
import { lockStatus, CODE_FILES } from "../src/codeHash.mjs";
import { scoreReceipt, aggregate } from "../src/scoring.mjs";

const FUEL = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const PNG = Buffer.from("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg==", "base64");
function sandbox() {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), "fuel-intake-"));
  for (const d of ["corpus", "verification", "intake", "src", "in"]) fs.mkdirSync(path.join(dir, d), { recursive: true });
  fs.writeFileSync(path.join(dir, "corpus", "annotations.json"), JSON.stringify({ records: [] }));
  for (const f of CODE_FILES) fs.writeFileSync(path.join(dir, f), `// dummy ${f}\n`);
  return dir;
}
const prov = (o = {}) => ({
  source_description: "test fixture", source_type: "authentic_issued_receipt", owner: "TEST OWNER", permission_basis: "owner permission (test)", authorized_by: "TEST AUTHORIZER, role", retrieval_date: "2026-10-21",
  brand_family: "Nayara", receipt_family: "nayara_layout_A", personal_data_handling: "none present", permitted_usage: { store: true, modify: false, evaluate: true, redistribute: false }, ...o,
});
function register(dir, name, provenance, split = "heldout_untouched", bytes = PNG) {
  const img = path.join(dir, "in", `${name}.png`), pj = path.join(dir, "in", `${name}.json`);
  fs.writeFileSync(img, Buffer.concat([bytes, Buffer.from(name)])); // distinct bytes per name
  fs.writeFileSync(pj, JSON.stringify(provenance));
  return execFileSync("node", [path.join(FUEL, "scripts", "register_intake.mjs"), img, "--provenance", pj, "--split", split, "--fuel-dir", dir], { stdio: "pipe" }).toString();
}
const refused = (fn, re) => assert.throws(fn, (e) => re.test(String(e.stderr || e.message)));

test("intake refuses generator/synthetic sources, missing provenance, and uses that may not be stored or evaluated", () => {
  const dir = sandbox();
  refused(() => register(dir, "a", prov({ source_type: "synthetic_template" })), /authentic_issued_receipt/);
  refused(() => register(dir, "b", prov({ authorized_by: "" })), /authorized_by is required/);
  refused(() => register(dir, "c", prov({ permitted_usage: { store: true, evaluate: false, modify: false, redistribute: false } })), /evaluate must be true/);
  refused(() => register(dir, "d", prov({ permitted_usage: { store: false, evaluate: true, modify: false, redistribute: false } })), /store must be true/);
  refused(() => register(dir, "e", prov({ brand_family: "Acme" })), /brand_family must be one of/);
  refused(() => register(dir, "f", prov({ contains_confidential_company_data: true, permitted_usage: { store: true, evaluate: true, modify: true, redistribute: true } })), /confidential/);
  assert.ok(!fs.existsSync(path.join(dir, "intake", "intake_register.json")), "nothing was registered");
});

test("a registered receipt is PENDING a human label, carries no label, and is never scored on a guess", () => {
  const dir = sandbox();
  register(dir, "n1", prov());
  const reg = JSON.parse(fs.readFileSync(path.join(dir, "intake", "intake_register.json"), "utf8"));
  assert.equal(reg.receipts.length, 1);
  assert.equal(reg.receipts[0].label_status, "pending_human_label");
  assert.equal(reg.receipts[0].split, "heldout_untouched");
  const { records } = loadAnnotations(dir);
  const r = records.find((x) => x.receipt_id === reg.receipts[0].receipt_id);
  assert.equal(r.split, "intake_heldout");
  assert.equal(r.annotation_status, "pending_human_label");
  assert.ok(Object.values(r.fields).every((f) => f.excluded && f.normalized_value === null));
  const pred = { pump_name: { normalized_value: "ANYTHING", review_required: false }, transaction_date: { normalized_value: "2026-01-01", review_required: false }, total_amount: { normalized_value: 5, review_required: false }, overall_review_required: false };
  assert.equal(aggregate([scoreReceipt(pred, r)]).receipts, 0, "an unlabelled intake receipt is excluded from every metric");
});

test("exact duplicates and split-conflicting families are refused", () => {
  const dir = sandbox();
  register(dir, "n1", prov());
  const img = path.join(dir, "in", "n1.png");
  refused(() => execFileSync("node", [path.join(FUEL, "scripts", "register_intake.mjs"), img, "--provenance", path.join(dir, "in", "n1.json"), "--split", "heldout_untouched", "--fuel-dir", dir], { stdio: "pipe" }), /exact duplicate/);
  refused(() => register(dir, "n2", prov(), "development"), /goes wholly to one side/);
  register(dir, "n3", prov({ receipt_family: "nayara_layout_B" }), "development"); // a different family may sit on the other side
  assert.equal(JSON.parse(fs.readFileSync(path.join(dir, "intake", "intake_register.json"), "utf8")).receipts.length, 2);
});

test("the held-out lock notices ANY later change to the extractor: the set is then development data", () => {
  const dir = sandbox();
  register(dir, "n1", prov());
  execFileSync("node", [path.join(FUEL, "scripts", "lock_heldout.mjs"), "--fuel-dir", dir], { stdio: "pipe" });
  assert.deepEqual({ ...lockStatus(dir), locked_at: undefined }, { locked: true, locked_at: undefined, still_untouched: true, changed_files: [], receipts: 1 });
  fs.appendFileSync(path.join(dir, "src", "fuelExtractor.mjs"), "// a parser change after the lock\n");
  const s = lockStatus(dir);
  assert.equal(s.still_untouched, false);
  assert.deepEqual(s.changed_files, ["src/fuelExtractor.mjs"]);
});

test("a verified intake receipt becomes scorable only through a valid human overlay", () => {
  const dir = sandbox();
  register(dir, "n1", prov());
  const id = JSON.parse(fs.readFileSync(path.join(dir, "intake", "intake_register.json"), "utf8")).receipts[0].receipt_id;
  const exp = { schema: "fuel-review-export/v1", reviewer_id: "TEST-REVIEWER", authorization_confirmed: true, exported_at: "2026-10-21T12:00:00Z", records: { [id]: {
    pump_name: { status: "read", value: "TEST NAYARA DEALER" }, transaction_date: { status: "read", printed_text: "24/09/2026", iso_date: "2026-09-24", order_provable_from_receipt: true, order_evidence: "component_gt_12" }, total_amount: { status: "read", value: 1234.5 } } } };
  fs.writeFileSync(path.join(dir, "in", "exp.json"), JSON.stringify(exp));
  execFileSync("node", [path.join(FUEL, "scripts", "apply_verification.mjs"), path.join(dir, "in", "exp.json"), "--annotations-dir", dir], { stdio: "pipe" });
  const r = loadAnnotations(dir).records.find((x) => x.receipt_id === id);
  assert.equal(r.annotation_status, "human_verified");
  assert.equal(r.fields.total_amount.normalized_value, 1234.5);
  assert.ok(!r.fields.total_amount.excluded);
});
