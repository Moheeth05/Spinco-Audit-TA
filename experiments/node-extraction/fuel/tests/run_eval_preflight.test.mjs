// Evaluation integrity: scripts/run_eval.mjs must refuse - exit 1, before ANY OCR, before ANY report or detail file is written - when a required image is missing, empty or
// modified, when the intake register is invalid, when held-out intake receipts are selected without a valid lock or the register disagrees with the lock, when nothing is
// selected, or when a command-line value is unknown. Valid inputs keep the existing behaviour (same groups, same metrics, same two report files).
// Synthetic only: each test runs the mirror's COPY of run_eval.mjs, whose OCR and extractor modules are mirror-local STUBS (tests/helpers/evalMirror.mjs). The stub OCR logs its
// construction and every read to <mirror>/ocr_stub_calls.log, so "OCR never started" is proven by that file not existing. No real corpus, report, lock or receipt is read.
import test from "node:test";
import assert from "node:assert/strict";
import fs from "node:fs";
import path from "node:path";
import { spawnSync } from "node:child_process";
import { assertIsolated, runScript, writeRegister, writeRegisterRaw, treeState, intakeEntry, placeholderImageBytes, sha256Hex, applyExports, exportFile, goodEntry, readJson, REAL_FUEL } from "./helpers/syntheticMirror.mjs";
import { makeEvalMirror, runEval, corpusImage } from "./helpers/evalMirror.mjs";
import * as EP from "../src/evalPreflight.mjs";

const HELD = { split: "heldout_untouched" };
const NO_CRASH = /TypeError|SyntaxError|ReferenceError|RangeError|at .*\.mjs:\d+/;
const regOf = (...receipts) => ({ note: "SYNTHETIC TEST FIXTURE", receipts });
const fresh = () => { const m = makeEvalMirror(); assertIsolated(m.root); return m; };
const lockIt = (m) => { const r = runScript(m.root, "lock_heldout.mjs"); assert.equal(r.status, 0, r.all); };
const lockPath = (m) => path.join(m.root, "intake", "heldout_lock.json");
const regPath = (m) => path.join(m.root, "intake", "intake_register.json");
const intakeImg = (m, id) => path.join(m.root, "corpus", "authentic_intake", "pending", `${id}.png`);
const readLog = (m) => (fs.existsSync(m.ocrLog) ? fs.readFileSync(m.ocrLog, "utf8").trim().split("\n") : []);
const listing = (dir) => fs.readdirSync(dir).sort().map((n) => `${n}|${fs.statSync(path.join(dir, n)).size}|${fs.statSync(path.join(dir, n)).mtimeMs}`);
const REAL_REPORTS_BEFORE = listing(path.join(REAL_FUEL, "reports"));

/** The evaluation must refuse before OCR and before any write: exit 1, REFUSED message with every fragment, no OCR log, and a mirror that is byte-identical afterwards. */
function assertRefused(label, m, args, fragments = [], before = treeState(m.root)) {
  const r = runEval(m, args);
  assert.equal(r.status, 1, `${label}: must exit 1 - ${r.all}`);
  assert.match(r.err, /REFUSED - evaluation not started \(no OCR was run, no report or detail file was written\)/, label);
  for (const f of fragments) assert.ok(r.err.includes(f), `${label}: expected "${f}" in:\n${r.err}`);
  assert.doesNotMatch(r.all, NO_CRASH, label);
  assert.ok(!fs.existsSync(m.ocrLog), `${label}: OCR must not have started (the stub OCR logs its construction and every read)`);
  assert.deepEqual(treeState(m.root), before, `${label}: nothing may be written (no report, no detail file, no cache, no log)`);
  return r;
}
const mkIntake = (m, entries, omit = []) => writeRegister(m, regOf(...entries), omit);

test("sanity: the mirror runs the real run_eval.mjs copy with stubbed OCR, and the isolation holds", () => {
  const m = fresh();
  assert.equal(fs.readFileSync(path.join(m.root, "scripts", "run_eval.mjs"), "utf8"), fs.readFileSync(path.join(REAL_FUEL, "scripts", "run_eval.mjs"), "utf8"));
  assert.match(fs.readFileSync(path.join(m.root, "src", "ocr.mjs"), "utf8"), /STUB OCR/);
  assert.ok(!fs.existsSync(m.reports) && !fs.existsSync(m.ocrLog));
});

test("11. valid development inputs keep the existing behaviour: same groups, known metrics, both report files, the selected records OCR'd once each, in order", () => {
  const m = fresh();
  const r = runEval(m, ["--impl", "improved", "--only", "dev", "--label", "dev1"]);
  assert.equal(r.status, 0, r.all);
  assert.deepEqual(fs.readdirSync(m.reports).sort(), ["dev1.details.json", "dev1.json"]);
  const rep = readJson(path.join(m.reports, "dev1.json"));
  assert.deepEqual([rep.label, rep.impl, rep.ocr_mode, rep.label_summary.mode, rep.label_summary.human_verified], ["dev1", "improved", "retry2", "all", 0]);
  assert.deepEqual(Object.keys(rep.groups), ["authentic_dev"], "--only dev: held-out, template, control and synthetic-held-out groups are not run");
  const g = rep.groups.authentic_dev;
  assert.equal(g.receipts, 2);
  assert.deepEqual([g.pump_name.exact_rate.n, g.pump_name.exact_rate.d, g.transaction_date.exact_rate.n, g.total_amount.exact_rate.n, g.total_amount.exact_rate.d], [2, 2, 2, 1, 2]);
  assert.deepEqual([g.overall.false_accept_rate.n, g.overall.true_accept.n], [1, 1]);
  assert.deepEqual(readLog(m), ["construct", "read SYNTH-AUTH-001.png retry2", "read SYNTH-AUTH-002.png retry2"]);
  assert.deepEqual(Object.keys(readJson(path.join(m.reports, "dev1.details.json"))), ["authentic_dev"]);
  assert.match(r.out, /=== dev1 \(improved\) ===/);
});

test("11b. default --only all on valid synthetic inputs: every group, five records OCR'd, no extra files", () => {
  const m = fresh();
  const r = runEval(m, ["--impl", "improved", "--label", "all1"]);
  assert.equal(r.status, 0, r.all);
  assert.deepEqual(Object.keys(readJson(path.join(m.reports, "all1.json")).groups).sort(), ["authentic_dev", "generic_controls", "suspected_template_generated", "synthetic_heldout"]);
  assert.equal(readLog(m).filter((l) => l.startsWith("read ")).length, 5);
  assert.deepEqual(fs.readdirSync(m.reports).sort(), ["all1.details.json", "all1.json"]);
});

test("2.+3. a missing or EMPTY image refuses the evaluation (corpus images and intake images); an image of a group that is not selected does not", () => {
  for (const [what, damage] of [["missing", (f) => fs.rmSync(f)], ["empty", (f) => fs.writeFileSync(f, "")]]) {
    const reason = what === "missing" ? "image file not found" : "image unavailable (empty file)";
    for (const [label, file, args] of [["authentic corpus image", (m) => corpusImage(m, "SYNTH-AUTH-001"), ["--only", "dev"]], ["synthetic corpus image", (m) => path.join(m.root, "corpus", "synthetic", "images", "SYNTH-FUEL-001.png"), []]]) {
      const m = fresh();
      damage(file(m));
      assertRefused(`${what} ${label}`, m, ["--impl", "improved", "--label", "x", ...args], [`receipt ${label.startsWith("authentic") ? "SYNTH-AUTH-001" : "SYNTH-FUEL-001"}: ${reason}`]);
    }
    const m = fresh();
    mkIntake(m, [intakeEntry("SYNTH-INTAKE-D1")]);
    damage(intakeImg(m, "SYNTH-INTAKE-D1"));
    assertRefused(`${what} intake image`, m, ["--impl", "improved", "--only", "dev", "--label", "x"], [`receipt SYNTH-INTAKE-D1: ${reason}`]);
  }
  const m = fresh();
  fs.rmSync(path.join(m.root, "corpus", "synthetic", "images", "SYNTH-FUEL-001.png"));
  const ok = runEval(m, ["--impl", "improved", "--only", "dev", "--label", "dev2"]);
  assert.equal(ok.status, 0, "SYNTH-FUEL-001 is not part of --only dev, so its missing image is not required: " + ok.all);
});

test("4. an intake image modified after registration refuses the evaluation (hash mismatch against the register)", () => {
  const m = fresh();
  mkIntake(m, [intakeEntry("SYNTH-INTAKE-D1")]);
  fs.appendFileSync(intakeImg(m, "SYNTH-INTAKE-D1"), "-modified-after-registration");
  assertRefused("modified dev image", m, ["--impl", "improved", "--only", "dev", "--label", "x"], ["receipt SYNTH-INTAKE-D1: image integrity check failed"]);
});

test("5. a held-out image modified AFTER the lock was created refuses a held-out evaluation - both by the register hash and, if the register was edited to match, by the LOCK", () => {
  const setup = () => { const m = fresh(); mkIntake(m, [intakeEntry("SYNTH-INTAKE-H1", { receipt_family: "synthetic_fam_H", ...HELD }), intakeEntry("SYNTH-INTAKE-D1")]); lockIt(m); assert.equal(runEval(m, ["--impl", "improved", "--label", "pre"]).status, 0, "valid before tampering"); fs.rmSync(m.reports, { recursive: true }); fs.rmSync(m.ocrLog); return m; };
  const m = setup();
  fs.appendFileSync(intakeImg(m, "SYNTH-INTAKE-H1"), "-tampered");
  assertRefused("image changed, register unchanged", m, ["--impl", "improved", "--label", "x"], ["receipt SYNTH-INTAKE-H1: image integrity check failed"]);
  const m2 = setup();
  fs.appendFileSync(intakeImg(m2, "SYNTH-INTAKE-H1"), "-tampered");
  const reg = readJson(regPath(m2));
  reg.receipts.find((e) => e.receipt_id === "SYNTH-INTAKE-H1").sha256 = sha256Hex(fs.readFileSync(intakeImg(m2, "SYNTH-INTAKE-H1"))); // register edited to match the modified image
  writeRegisterRaw(m2, JSON.stringify(reg));
  assertRefused("image AND register changed, lock unchanged", m2, ["--impl", "improved", "--label", "x"], ["receipt SYNTH-INTAKE-H1: the register's sha256 differs from the sha256 recorded in the held-out lock"]);
  const dev = runEval(m2, ["--impl", "improved", "--only", "dev", "--label", "devonly"]);
  assert.equal(dev.status, 0, "a development-only evaluation does not involve the held-out set: " + dev.all);
});

test("6. no lock when held-out intake receipts are selected refuses (development-only is unaffected); a lock that EXISTS but is malformed refuses whatever is selected", () => {
  const m = fresh();
  mkIntake(m, [intakeEntry("SYNTH-INTAKE-H1", { receipt_family: "synthetic_fam_H", ...HELD }), intakeEntry("SYNTH-INTAKE-D1")]);
  assertRefused("no lock", m, ["--impl", "improved", "--label", "x"], ["there is no held-out lock", "scripts/lock_heldout.mjs"]);
  assertRefused("no lock, --only heldout", m, ["--impl", "improved", "--only", "heldout", "--label", "x"], ["there is no held-out lock"]);
  assert.equal(runEval(m, ["--impl", "improved", "--only", "dev", "--label", "d"]).status, 0, "development-only needs no lock");
  const bad = ["{ not json", "", "[]", "null", "{}", JSON.stringify({ locked_at: "2026-10-22T00:00:00Z", statement: "s", code_sha256: { "src/a.mjs": "0".repeat(64) } }),
    JSON.stringify({ locked_at: "x", statement: "s", code_sha256: { "src/a.mjs": "0".repeat(64) }, heldout_receipts: [] }),
    JSON.stringify({ locked_at: "2026-10-22T00:00:00Z", statement: "s", code_sha256: { "src/a.mjs": "zz" }, heldout_receipts: [] }),
    JSON.stringify({ locked_at: "2026-10-22T00:00:00Z", statement: "s", code_sha256: { "src/a.mjs": "0".repeat(64) }, heldout_receipts: [{ id: "A", family: "f", brand: "b", sha256: "short" }] }),
    JSON.stringify({ locked_at: "2026-10-22T00:00:00Z", statement: "s", code_sha256: { "src/a.mjs": "0".repeat(64) }, heldout_receipts: [{ id: "A", family: "f", brand: "b", sha256: "1".repeat(64) }, { id: "A", family: "f", brand: "b", sha256: "2".repeat(64) }] })];
  for (const text of bad) {
    const mm = fresh();
    mkIntake(mm, [intakeEntry("SYNTH-INTAKE-H1", { receipt_family: "synthetic_fam_H", ...HELD }), intakeEntry("SYNTH-INTAKE-D1")]);
    fs.writeFileSync(lockPath(mm), text);
    for (const only of ["all", "dev"]) assertRefused(`malformed lock ${JSON.stringify(text).slice(0, 40)} --only ${only}`, mm, ["--impl", "improved", "--only", only, "--label", "x"], ["intake/heldout_lock.json"]);
  }
  const md = fresh();
  mkIntake(md, [intakeEntry("SYNTH-INTAKE-H1", { receipt_family: "synthetic_fam_H", ...HELD })]);
  fs.mkdirSync(lockPath(md));
  assertRefused("lock is a directory", md, ["--impl", "improved", "--label", "x"], ["not a regular file"]);
});

test("7. lock/register mismatch refuses: register entry added after the lock, a receipt only in the lock, and a changed hash / family / brand", () => {
  const base = () => { const m = fresh(); mkIntake(m, [intakeEntry("SYNTH-INTAKE-H1", { receipt_family: "synthetic_fam_H", brand_family: "Nayara", ...HELD }), intakeEntry("SYNTH-INTAKE-D1")]); lockIt(m); return m; };
  let m = base();
  mkIntake(m, [intakeEntry("SYNTH-INTAKE-H1", { receipt_family: "synthetic_fam_H", brand_family: "Nayara", ...HELD }), intakeEntry("SYNTH-INTAKE-H2", { receipt_family: "synthetic_fam_H2", ...HELD }), intakeEntry("SYNTH-INTAKE-D1")]);
  assertRefused("held-out entry added after the lock", m, ["--impl", "improved", "--label", "x"], ["receipt SYNTH-INTAKE-H2: is a held-out entry in the register but is not in the held-out lock"]);
  m = base();
  const lock = readJson(lockPath(m)); lock.heldout_receipts.push({ id: "SYNTH-INTAKE-GHOST", family: "synthetic_fam_G", brand: "other", sha256: "9".repeat(64) });
  fs.writeFileSync(lockPath(m), JSON.stringify(lock));
  assertRefused("receipt only in the lock", m, ["--impl", "improved", "--label", "x"], ["the held-out lock lists receipt SYNTH-INTAKE-GHOST"]);
  for (const [label, edit, fragment] of [["hash", (e) => { e.sha256 = "8".repeat(64); }, "sha256 differs"], ["family", (e) => { e.receipt_family = "synthetic_fam_OTHER"; }, "receipt_family differs"], ["brand", (e) => { e.brand_family = "Shell"; }, "brand_family differs"]]) {
    const mm = base();
    const reg = readJson(regPath(mm)); edit(reg.receipts.find((e) => e.receipt_id === "SYNTH-INTAKE-H1"));
    writeRegisterRaw(mm, JSON.stringify(reg));
    assertRefused(`register ${label} changed after locking`, mm, ["--impl", "improved", "--label", "x"], [fragment]);
  }
});

test("8. an invalid register, conflicting duplicates and cross-split families refuse the evaluation - even a development-only one (silently dropping intake receipts would mislead)", () => {
  const P = "corpus/authentic_intake/pending/shared.png";
  const sets = [
    ["cross-split family", [intakeEntry("SYNTH-INTAKE-A", { receipt_family: "synthetic_fam_X" }), intakeEntry("SYNTH-INTAKE-B", { receipt_family: "synthetic_fam_X", ...HELD })], "appears under both development and heldout_untouched"],
    ["duplicate path", [intakeEntry("SYNTH-INTAKE-A", { image_path: P }), intakeEntry("SYNTH-INTAKE-B", { image_path: P, receipt_family: "synthetic_fam_B" })], "image_path appears on more than one"],
    ["duplicate hash", [intakeEntry("SYNTH-INTAKE-A", { sha256: "a".repeat(64) }), intakeEntry("SYNTH-INTAKE-B", { sha256: "a".repeat(64), receipt_family: "synthetic_fam_B" })], "sha256 appears on more than one"],
    ["malformed entry", [intakeEntry("SYNTH-INTAKE-A"), intakeEntry("SYNTH-INTAKE-B", { receipt_family: "synthetic_fam_B", provenance: null })], "provenance is missing or malformed"],
  ];
  for (const [label, entries, fragment] of sets) {
    const m = fresh();
    mkIntake(m, entries);
    for (const only of ["dev", "all"]) assertRefused(`${label} --only ${only}`, m, ["--impl", "improved", "--only", only, "--label", "x"], [fragment]);
  }
  for (const text of ["{ not json", "[]", '{"receipts":"x"}']) {
    const m = fresh();
    writeRegisterRaw(m, text);
    assertRefused(`unusable register ${text}`, m, ["--impl", "improved", "--only", "dev", "--label", "x"], ["intake register is invalid"]);
  }
});

test("9. an evaluation request with no eligible records refuses instead of writing an empty report", () => {
  for (const only of ["heldout2", "heldout3", "heldout4"]) assertRefused(`--only ${only}`, fresh(), ["--impl", "improved", "--only", only, "--label", "x"], ["no eligible records were selected"]);
});

test("unknown or unsafe command-line values refuse before anything else happens (they used to be silent defaults, or file names)", () => {
  const bad = [["--only", "nonsense"], ["--only", "dev2"], ["--impl", "nonsense"], ["--ocr", "nonsense"], ["--labels", "nonsense"], ["--label", "../escape"], ["--label", "a/b"], ["--label", "a\\b"], ["--label", ".hidden"], ["--label", "x".repeat(90)]];
  for (const [flag, value] of bad) {
    const m = fresh();
    const args = ["--impl", "improved", "--label", "x", flag, value];
    assertRefused(`${flag} ${JSON.stringify(value)}`, m, args, [flag]);
  }
  assert.deepEqual(EP.argProblems({ impl: "improved", only: "all", label: "improved-phase2-FINAL4", ocrMode: "retry2", labels: undefined }), []);
  assert.deepEqual(EP.argProblems({ impl: "baseline", only: "heldout4", label: "baseline-phase2-HELDOUT4", ocrMode: "single", labels: "verified_only" }), []);
});

test("10. failure happens before OCR starts and before any report is written - guarded in the SOURCE order too (the real CachedOCR constructor itself creates a folder)", () => {
  const src = fs.readFileSync(path.join(REAL_FUEL, "scripts", "run_eval.mjs"), "utf8");
  const at = (s) => { const i = src.indexOf(s); assert.ok(i >= 0, `run_eval.mjs must contain ${s}`); return i; };
  assert.ok(at("argProblems(") < at('import("../src/fuelExtractor.mjs")'), "arguments are checked before any module is loaded");
  assert.ok(at("evalPreflightProblems(") < at("new CachedOCR("), "preflight runs before the OCR object (whose constructor mkdirs the cache) exists");
  assert.ok(at("new CachedOCR(") < at("await ocr.read("), "OCR reads come after construction");
  assert.ok(at("evalPreflightProblems(") < at('path.join(FUEL, "reports")'), "preflight runs before anything touches reports/");
  assert.ok(at("REFUSED - evaluation not started") > 0);
});

test("12. existing reports are never touched: a refusal leaves them byte-identical and a valid run with another label leaves them alone", () => {
  const m = fresh();
  fs.mkdirSync(m.reports);
  fs.writeFileSync(path.join(m.reports, "existing.json"), '{"sentinel":"existing report"}');
  fs.writeFileSync(path.join(m.reports, "existing.details.json"), '{"sentinel":"existing details"}');
  fs.rmSync(corpusImage(m, "SYNTH-AUTH-001"));
  assertRefused("refusal with existing reports present", m, ["--impl", "improved", "--only", "dev", "--label", "existing"], ["receipt SYNTH-AUTH-001: image file not found"]);
  fs.writeFileSync(corpusImage(m, "SYNTH-AUTH-001"), placeholderImageBytes("SYNTH-AUTH-001"));
  const before = fs.readFileSync(path.join(m.reports, "existing.json"));
  assert.equal(runEval(m, ["--impl", "improved", "--only", "dev", "--label", "fresh"]).status, 0);
  assert.deepEqual(fs.readFileSync(path.join(m.reports, "existing.json")), before);
  assert.deepEqual(fs.readdirSync(m.reports).sort(), ["existing.details.json", "existing.json", "fresh.details.json", "fresh.json"]);
});

test("a register written by the REAL writer + a lock from the hardened lock script + verified labels: evaluation works and keeps its semantics (verified receipts scored, pending ones unscored)", () => {
  const m = fresh();
  const prov = (family) => ({ source_description: "synthetic fixture", source_type: "authentic_issued_receipt", owner: "TEST OWNER", permission_basis: "test", authorized_by: "TEST AUTHORIZER", retrieval_date: "2026-10-22", brand_family: "Nayara", receipt_family: family, personal_data_handling: "none", permitted_usage: { store: true, modify: false, evaluate: true, redistribute: false } });
  let n = 0;
  const register = (family, split) => {
    const img = path.join(m.root, "in", `rw-${++n}.png`), pj = path.join(m.root, "in", `rw-${n}.json`);
    fs.writeFileSync(img, Buffer.concat([placeholderImageBytes("real-writer"), Buffer.from(`distinct-${n}`)]));
    fs.writeFileSync(pj, JSON.stringify(prov(family)));
    for (const p of [img, pj]) assert.ok(path.resolve(p).startsWith(path.resolve(m.root) + path.sep));
    const w = spawnSync("node", [path.join(REAL_FUEL, "scripts", "register_intake.mjs"), img, "--provenance", pj, "--split", split, "--fuel-dir", m.root], { encoding: "utf8" });
    assert.equal(w.status, 0, w.stdout + w.stderr);
  };
  register("synthetic_rw_dev", "development"); register("synthetic_rw_held", "heldout_untouched");
  const [dev, held] = readJson(regPath(m)).receipts;
  assert.equal(applyExports(m.root, [exportFile("REV-001", { [dev.receipt_id]: goodEntry() })]).status, 0, "one development receipt is human-verified");
  lockIt(m);
  const r = runEval(m, ["--impl", "improved", "--label", "rw"]);
  assert.equal(r.status, 0, r.all);
  const rep = readJson(path.join(m.reports, "rw.json"));
  assert.equal(rep.groups.authentic_intake_dev.receipts, 1);
  assert.deepEqual(rep.groups.authentic_intake_dev.label_status, { human_verified: 1 });
  assert.equal(rep.groups.authentic_intake_heldout.receipts, 0);
  assert.equal(rep.groups.authentic_intake_heldout.unscored_labels_excluded, 1, "the pending held-out receipt is run but never scored");
  assert.equal(rep.heldout_intake_lock.locked, true);
  assert.equal(rep.heldout_intake_lock.receipts, 1);
  assert.ok(held.receipt_id);
});

test("pure helpers: lockProblems accepts the schema lock_heldout.mjs writes and rejects malformed ones; corpusImageProblem never throws", () => {
  const good = { locked_at: "2026-10-22T10:00:00.000Z", statement: "s", code_sha256: { "src/x.mjs": "a".repeat(64) }, heldout_receipts: [{ id: "A", family: "f", brand: "b", sha256: "b".repeat(64) }] };
  assert.deepEqual(EP.lockProblems(good), []);
  assert.deepEqual(EP.lockProblems({ ...good, heldout_receipts: [] }), [], "an old, empty lock is structurally readable (the set comparison then decides)");
  for (const bad of [null, [], "x", 5, {}, { ...good, locked_at: "nope" }, { ...good, statement: "" }, { ...good, code_sha256: {} }, { ...good, heldout_receipts: {} }, { ...good, heldout_receipts: [null] }, { ...good, heldout_receipts: [{ ...good.heldout_receipts[0], family: "" }] }]) assert.ok(EP.lockProblems(bad).length > 0, JSON.stringify(bad));
  const m = fresh();
  for (const [file, expected] of [[path.join(m.root, "corpus", "authentic", "images", "nope.png"), "image file not found"], [path.join(m.root, "..", "outside.png"), "image unavailable (resolves outside corpus/)"], [path.join(m.root, "corpus", "authentic", "images"), "image unavailable (not a regular file)"], [corpusImage(m, "SYNTH-AUTH-001"), null]]) assert.equal(EP.corpusImageProblem(m.root, file), expected);
  assert.equal(typeof EP.corpusImageProblem(path.join(m.root, "no-such-dir"), "x"), "string");
});

test("isolation: this file never touched the real reports/ folder (names, sizes and mtimes unchanged) and every mirror is outside the real corpus", () => {
  assert.deepEqual(listing(path.join(REAL_FUEL, "reports")), REAL_REPORTS_BEFORE);
  const m = fresh();
  assert.ok(path.relative(path.join(REAL_FUEL, "corpus"), m.root).startsWith("..") || path.isAbsolute(path.relative(path.join(REAL_FUEL, "corpus"), m.root)));
});
