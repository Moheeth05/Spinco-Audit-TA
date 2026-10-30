// Intake image INTEGRITY: the register's `sha256` (lowercase hex SHA-256 of the raw stored image bytes - scripts/register_intake.mjs) must match the file before the
// image can be offered in a review pack or accepted by the applier. Three separate properties (see verification/README.md): structural validity (pure; src/labels.mjs
// depends only on this), image availability (intakeImageProblem), image integrity (intakeImageIntegrityProblem = availability first, then the hash).
// Synthetic only: every test builds a mirror (tests/helpers/syntheticMirror.mjs) with invented ids and locally generated placeholder images.
import test from "node:test";
import assert from "node:assert/strict";
import fs from "node:fs";
import path from "node:path";
import { spawnSync } from "node:child_process";
import { pathToFileURL } from "node:url";
import { makeMirror, assertIsolated, applyExports, exportFile, goodEntry, readJson, writeRegister, buildPack, placeIntakeImages, treeState, intakeEntry, sha256Hex, PNG_1X1, PLACEHOLDER_SHA256, placeholderImageBytes, REAL_FUEL } from "./helpers/syntheticMirror.mjs";
import * as IR from "../src/intakeRegister.mjs";

const NO_CRASH = /TypeError|SyntaxError|ReferenceError|RangeError|at .*\.mjs:\d+/;
const MISMATCH = "image integrity check failed";
const MISSING = "image integrity unverifiable (sha256 is missing from the register entry)";
const MALFORMED = "image integrity unverifiable (sha256 is not 64 lowercase hex characters)";
const regOf = (...receipts) => ({ note: "SYNTHETIC TEST FIXTURE", receipts });
const fresh = () => { const m = makeMirror(); assertIsolated(m.root); return m; };
const imgAbs = (m, id) => path.join(m.root, "corpus", "authentic_intake", "pending", `${id}.png`);
const manifestOf = (m) => JSON.parse(fs.readFileSync(path.join(m.packDir, "manifest.js"), "utf8").replace(/^window\.REVIEW_MANIFEST = /, "").replace(/;\s*$/, ""));
const offered = (m) => manifestOf(m).items.map((x) => x.id).sort();
const labelsOf = (root) => import(pathToFileURL(path.join(root, "src", "labels.mjs")).href + `?t=${Math.random()}`);
const CHANGED = Buffer.concat([PNG_1X1, Buffer.from("-modified-after-registration")]);
const noWrite = (m, before, label) => assert.deepEqual(treeState(m.root), before, `${label}: the mirror must be byte-identical outside in/ - no label, history, conflict or adjudication write`);

test("integrity helper: matching hash -> null; modified (appended or same-size byte flip) -> mismatch; hash read from the SAME resolved file", () => {
  const m = fresh();
  writeRegister(m, regOf(intakeEntry("SYNTH-INTAKE-OK")));
  const e = intakeEntry("SYNTH-INTAKE-OK");
  assert.equal(IR.intakeImageIntegrityProblem(m.root, e), null);
  fs.writeFileSync(imgAbs(m, "SYNTH-INTAKE-OK"), CHANGED);
  assert.match(IR.intakeImageIntegrityProblem(m.root, e), new RegExp(MISMATCH));
  const original = placeholderImageBytes("SYNTH-INTAKE-OK");
  const flipped = Buffer.from(original); flipped[flipped.length - 1] ^= 0xff;
  assert.equal(flipped.length, original.length);
  fs.writeFileSync(imgAbs(m, "SYNTH-INTAKE-OK"), flipped);
  assert.match(IR.intakeImageIntegrityProblem(m.root, e), new RegExp(MISMATCH), "same size, one byte different");
  fs.writeFileSync(imgAbs(m, "SYNTH-INTAKE-OK"), original);
  assert.equal(IR.intakeImageIntegrityProblem(m.root, e), null, "restoring the original bytes restores acceptance");
});

test("integrity helper: a multi-megabyte file is hashed correctly across chunk boundaries", () => {
  const m = fresh();
  const dir = path.join(m.root, "corpus", "authentic_intake", "pending"); fs.mkdirSync(dir, { recursive: true });
  const big = Buffer.alloc(3 * 1024 * 1024 + 12345); for (let i = 0; i < big.length; i++) big[i] = (i * 31 + (i >> 8)) & 0xff;
  fs.writeFileSync(path.join(dir, "big.png"), big);
  const e = { image_path: "corpus/authentic_intake/pending/big.png", sha256: sha256Hex(big) };
  assert.equal(IR.intakeImageIntegrityProblem(m.root, e), null);
  assert.match(IR.intakeImageIntegrityProblem(m.root, { ...e, sha256: sha256Hex(Buffer.concat([big, Buffer.from("x")])) }), new RegExp(MISMATCH));
});

test("integrity helper: missing / malformed / non-normalised hashes are unverifiable (no case folding, trimming or rewriting)", () => {
  const m = fresh();
  writeRegister(m, regOf(intakeEntry("SYNTH-INTAKE-OK")));
  const base = intakeEntry("SYNTH-INTAKE-OK");
  const { sha256: _drop, ...noHash } = base; // eslint-disable-line no-unused-vars
  assert.equal(IR.intakeImageIntegrityProblem(m.root, noHash), MISSING);
  assert.equal(IR.intakeImageIntegrityProblem(m.root, { ...base, sha256: null }), MISSING);
  for (const bad of ["", "xyz", "0".repeat(63), "0".repeat(65), PLACEHOLDER_SHA256.toUpperCase(), ` ${PLACEHOLDER_SHA256}`, `${PLACEHOLDER_SHA256}\n`, `sha256:${PLACEHOLDER_SHA256}`, "g".repeat(64), 5, true, [], [PLACEHOLDER_SHA256], {}]) {
    assert.equal(IR.intakeImageIntegrityProblem(m.root, { ...base, sha256: bad }), MALFORMED, `sha256 ${JSON.stringify(bad)}`);
  }
  assert.match(IR.intakeImageIntegrityProblem(m.root, { ...base, sha256: "0".repeat(64) }), new RegExp(MISMATCH), "well-formed but wrong -> mismatch, not malformed");
});

test("integrity helper: availability problems are reported first and unchanged (missing, directory, zero-byte, outside the intake folder, hostile input) even with a matching hash", () => {
  const m = fresh();
  const dir = path.join(m.root, "corpus", "authentic_intake", "pending"); fs.mkdirSync(dir, { recursive: true });
  const e = (name, sha = PLACEHOLDER_SHA256) => ({ image_path: `corpus/authentic_intake/pending/${name}`, sha256: sha });
  assert.equal(IR.intakeImageIntegrityProblem(m.root, e("absent.png")), "image file not found");
  fs.mkdirSync(path.join(dir, "adir.png"));
  assert.equal(IR.intakeImageIntegrityProblem(m.root, e("adir.png")), "image unavailable (not a regular file)");
  fs.writeFileSync(path.join(dir, "zero.png"), "");
  assert.equal(IR.intakeImageIntegrityProblem(m.root, e("zero.png", sha256Hex(Buffer.alloc(0)))), "image unavailable (empty file)", "even the hash of an empty file does not make an empty image acceptable");
  // a file OUTSIDE the intake folder whose bytes DO match the recorded hash is still rejected
  fs.writeFileSync(path.join(m.root, "outside.png"), PNG_1X1);
  assert.match(IR.intakeImageIntegrityProblem(m.root, { image_path: "corpus/authentic_intake/../../outside.png", sha256: PLACEHOLDER_SHA256 }), /image unavailable/);
  const outsideDir = path.join(m.root, "outside_dir"); fs.mkdirSync(outsideDir); fs.writeFileSync(path.join(outsideDir, "a.png"), PNG_1X1);
  fs.symlinkSync(outsideDir, path.join(dir, "escape"), "junction");
  assert.equal(IR.intakeImageIntegrityProblem(m.root, e("escape/a.png")), "image unavailable (resolves outside corpus/authentic_intake/)");
  for (const entry of [null, undefined, 7, "s", [], {}, { image_path: 5, sha256: PLACEHOLDER_SHA256 }]) assert.equal(typeof IR.intakeImageIntegrityProblem(m.root, entry), "string");
  assert.equal(typeof IR.intakeImageIntegrityProblem(path.join(m.root, "no-such-dir"), e("ok.png")), "string");
  // the availability-only helper is unchanged
  fs.writeFileSync(path.join(dir, "changed.png"), CHANGED);
  assert.equal(IR.intakeImageProblem(m.root, e("changed.png")), null, "intakeImageProblem still checks availability only");
});

test("loader stays structural: an entry with a mismatching, missing or malformed hash still loads as a PENDING record (labels.mjs never reads image contents)", async () => {
  const m = fresh();
  const { sha256: _d, ...noHash } = intakeEntry("SYNTH-INTAKE-NOHASH"); // eslint-disable-line no-unused-vars
  writeRegister(m, regOf(intakeEntry("SYNTH-INTAKE-BAD", { sha256: "0".repeat(64) }), noHash, intakeEntry("SYNTH-INTAKE-MAL", { sha256: "zz" })));
  const L = await labelsOf(m.root);
  const { records, summary } = L.loadAnnotations(m.root);
  assert.equal(summary.skipped_intake_entries, 0);
  for (const id of ["SYNTH-INTAKE-BAD", "SYNTH-INTAKE-NOHASH", "SYNTH-INTAKE-MAL"]) assert.equal(records.find((r) => r.receipt_id === id).annotation_status, "pending_human_label");
});

test("applier: a matching hash is accepted; an image MODIFIED after registration is rejected naming the receipt, with nothing written; restoring the bytes restores acceptance", () => {
  const m = fresh();
  writeRegister(m, regOf(intakeEntry("SYNTH-INTAKE-OK")));
  fs.writeFileSync(imgAbs(m, "SYNTH-INTAKE-OK"), CHANGED);
  const before = treeState(m.root);
  const r = applyExports(m.root, [exportFile("REV-001", { "SYNTH-INTAKE-OK": goodEntry() })]);
  assert.equal(r.status, 1, r.all);
  assert.match(r.err, /REJECTED - nothing was written/);
  assert.ok(r.err.includes("receipt SYNTH-INTAKE-OK: image integrity check failed"), r.err);
  assert.doesNotMatch(r.all, NO_CRASH);
  noWrite(m, before, "modified image");
  for (const f of ["verified_labels.json", "review_history.json", "review_conflicts.json", "adjudication_needed.json"]) assert.ok(!fs.existsSync(path.join(m.root, "corpus", f)), f);
  fs.writeFileSync(imgAbs(m, "SYNTH-INTAKE-OK"), placeholderImageBytes("SYNTH-INTAKE-OK"));
  const ok = applyExports(m.root, [exportFile("REV-001", { "SYNTH-INTAKE-OK": goodEntry() })]);
  assert.equal(ok.status, 0, ok.all);
  assert.deepEqual(Object.keys(readJson(m.verifiedFile).records), ["SYNTH-INTAKE-OK"]);
});

test("missing or malformed hash: neither offered in a pack nor accepted by the applier; the valid neighbour is unaffected", () => {
  const { sha256: _d, ...noHash } = intakeEntry("SYNTH-INTAKE-BADHASH"); // eslint-disable-line no-unused-vars
  const cases = [["missing", noHash, MISSING], ["null", intakeEntry("SYNTH-INTAKE-BADHASH", { sha256: null }), MISSING], ["empty", intakeEntry("SYNTH-INTAKE-BADHASH", { sha256: "" }), MALFORMED], ["short", intakeEntry("SYNTH-INTAKE-BADHASH", { sha256: "abc" }), MALFORMED], ["uppercase", intakeEntry("SYNTH-INTAKE-BADHASH", { sha256: PLACEHOLDER_SHA256.toUpperCase() }), MALFORMED], ["number", intakeEntry("SYNTH-INTAKE-BADHASH", { sha256: 5 }), MALFORMED], ["wrong but well formed", intakeEntry("SYNTH-INTAKE-BADHASH", { sha256: "1".repeat(64) }), MISMATCH]];
  for (const [label, entry, reason] of cases) {
    const m = fresh();
    writeRegister(m, regOf(intakeEntry("SYNTH-INTAKE-OK"), entry));
    const g = buildPack(m, ["--scope", "all"]);
    assert.equal(g.status, 0, `${label}: ${g.all}`);
    assert.doesNotMatch(g.all, NO_CRASH);
    assert.ok(!offered(m).includes("SYNTH-INTAKE-BADHASH"), `${label}: not offered`);
    assert.ok(offered(m).includes("SYNTH-INTAKE-OK"), `${label}: neighbour offered`);
    assert.ok(g.err.includes(`intake receipt SYNTH-INTAKE-BADHASH: not offered (${reason}`), `${label}: ${g.err}`);
    const before = treeState(m.root);
    const bad = applyExports(m.root, [exportFile("REV-001", { "SYNTH-INTAKE-BADHASH": goodEntry() })]);
    assert.equal(bad.status, 1, `${label}: ${bad.all}`);
    assert.ok(bad.err.includes(`receipt SYNTH-INTAKE-BADHASH: ${reason}`), `${label}: ${bad.err}`);
    assert.doesNotMatch(bad.all, NO_CRASH);
    noWrite(m, before, label);
    assert.equal(applyExports(m.root, [exportFile("REV-001", { "SYNTH-INTAKE-OK": goodEntry() })]).status, 0, `${label}: the valid neighbour is accepted`);
  }
});

test("missing image, zero-byte image and an image outside the intake directory stay rejected (a matching hash does not rescue them)", () => {
  const m = fresh();
  const reg = regOf(intakeEntry("SYNTH-INTAKE-NOFILE"), intakeEntry("SYNTH-INTAKE-EMPTY", { sha256: sha256Hex(Buffer.alloc(0)) }), intakeEntry("SYNTH-INTAKE-ESC", { image_path: "corpus/authentic_intake/pending/escape/a.png" }));
  writeRegister(m, reg, ["SYNTH-INTAKE-NOFILE", "SYNTH-INTAKE-EMPTY", "SYNTH-INTAKE-ESC"]);
  fs.mkdirSync(path.dirname(imgAbs(m, "SYNTH-INTAKE-EMPTY")), { recursive: true }); // every image was omitted, so the folder does not exist yet
  fs.writeFileSync(imgAbs(m, "SYNTH-INTAKE-EMPTY"), "");
  const outsideDir = path.join(m.root, "outside_dir"); fs.mkdirSync(outsideDir, { recursive: true });
  fs.writeFileSync(path.join(outsideDir, "a.png"), PNG_1X1); // bytes match the recorded hash, but the file is outside the intake folder
  fs.symlinkSync(outsideDir, path.join(m.root, "corpus", "authentic_intake", "pending", "escape"), "junction");
  const before = treeState(m.root);
  const want = { "SYNTH-INTAKE-NOFILE": "image file not found", "SYNTH-INTAKE-EMPTY": "image unavailable (empty file)", "SYNTH-INTAKE-ESC": "image unavailable (resolves outside corpus/authentic_intake/)" };
  for (const [id, reason] of Object.entries(want)) {
    const r = applyExports(m.root, [exportFile("REV-001", { [id]: goodEntry() })]);
    assert.equal(r.status, 1, `${id}: ${r.all}`);
    assert.ok(r.err.includes(`receipt ${id}: ${reason}`), `${id}: ${r.err}`);
  }
  assert.deepEqual(treeState(m.root), before, "the three refusals wrote nothing");
  assert.ok(buildPack(m, ["--scope", "pending"]).status === 0 && offered(m).length === 0, "none of them is offered");
});

test("a mixed export (one valid image, one bad hash) rejects entirely and writes nothing - also together with an ineligible id", () => {
  const m = fresh();
  writeRegister(m, regOf(intakeEntry("SYNTH-INTAKE-OK"), intakeEntry("SYNTH-INTAKE-BAD", { sha256: "2".repeat(64) })));
  const before = treeState(m.root);
  const r = applyExports(m.root, [exportFile("REV-001", { "SYNTH-INTAKE-OK": goodEntry(), "SYNTH-INTAKE-BAD": goodEntry() })]);
  assert.equal(r.status, 1, r.all);
  assert.ok(r.err.includes("receipt SYNTH-INTAKE-BAD: image integrity check failed") && !r.err.includes("receipt SYNTH-INTAKE-OK"), r.err);
  noWrite(m, before, "mixed export");
  const both = applyExports(m.root, [exportFile("REV-001", { "SYNTH-INTAKE-BAD": goodEntry(), "SYNTH-FUEL-001": goodEntry(), "SYNTH-AUTH-001": goodEntry() })]);
  assert.equal(both.status, 1);
  assert.ok(both.err.includes("image integrity check failed") && /not eligible/.test(both.err), "ineligible and unverifiable reported together");
  noWrite(m, before, "ineligible + bad hash");
});

test("an unrelated receipt is processed when the export does not name the entry with the bad hash", () => {
  const m = fresh();
  writeRegister(m, regOf(intakeEntry("SYNTH-INTAKE-OK"), intakeEntry("SYNTH-INTAKE-BAD", { sha256: "3".repeat(64) })));
  const r = applyExports(m.root, [exportFile("REV-001", { "SYNTH-INTAKE-OK": goodEntry(), "SYNTH-AUTH-001": goodEntry() })]);
  assert.equal(r.status, 0, r.all);
  assert.deepEqual(Object.keys(readJson(m.verifiedFile).records).sort(), ["SYNTH-AUTH-001", "SYNTH-INTAKE-OK"]);
});

test("a PREVIOUSLY VERIFIED entry whose image content later changes is not silently accepted (identical re-application, or a conflicting review) and nothing is written", () => {
  const m = fresh();
  writeRegister(m, regOf(intakeEntry("SYNTH-INTAKE-OK")));
  assert.equal(applyExports(m.root, [exportFile("REV-001", { "SYNTH-INTAKE-OK": goodEntry() })]).status, 0);
  fs.writeFileSync(imgAbs(m, "SYNTH-INTAKE-OK"), CHANGED); // content changed after verification
  const before = treeState(m.root);
  const same = applyExports(m.root, [exportFile("REV-001", { "SYNTH-INTAKE-OK": goodEntry() })]);
  assert.equal(same.status, 1, same.all);
  assert.ok(same.err.includes("receipt SYNTH-INTAKE-OK: image integrity check failed"));
  const conflicting = applyExports(m.root, [exportFile("REV-002", { "SYNTH-INTAKE-OK": { ...goodEntry(), total_amount: { status: "read", value: 999 } } })]);
  assert.equal(conflicting.status, 1, "rejected before conflict handling: exit 1, not the conflict exit 3");
  noWrite(m, before, "verified entry with changed image");
  assert.ok(!fs.existsSync(path.join(m.root, "corpus", "review_conflicts.json")), "no conflict event was recorded either");
  fs.writeFileSync(imgAbs(m, "SYNTH-INTAKE-OK"), placeholderImageBytes("SYNTH-INTAKE-OK"));
  assert.equal(applyExports(m.root, [exportFile("REV-001", { "SYNTH-INTAKE-OK": goodEntry() })]).status, 0, "original bytes restored: the identical re-application is the usual no-op");
});

test("pack and applier agree for every synthetic intake entry (scopes pending, all, authentic): offered <=> accepted; everything else refused with nothing written", () => {
  const m = fresh();
  const A = "SYNTH-INTAKE-A", B = "SYNTH-INTAKE-B";
  const { sha256: _d, ...noHash } = intakeEntry("SYNTH-INTAKE-NOHASH"); // eslint-disable-line no-unused-vars
  const reg = regOf(intakeEntry(A), intakeEntry(B, { split: "heldout_untouched", receipt_family: "synthetic_family_b" }), intakeEntry("SYNTH-INTAKE-WRONGHASH", { sha256: "4".repeat(64) }), noHash, intakeEntry("SYNTH-INTAKE-MALHASH", { sha256: "not-a-hash" }), intakeEntry("SYNTH-INTAKE-MODIFIED"), intakeEntry("SYNTH-INTAKE-NOFILE"), intakeEntry("SYNTH-INTAKE-EMPTY", { sha256: sha256Hex(Buffer.alloc(0)) }));
  writeRegister(m, reg, ["SYNTH-INTAKE-NOFILE", "SYNTH-INTAKE-EMPTY"]);
  fs.writeFileSync(imgAbs(m, "SYNTH-INTAKE-MODIFIED"), CHANGED);
  fs.writeFileSync(imgAbs(m, "SYNTH-INTAKE-EMPTY"), "");
  const intakeIds = reg.receipts.map((x) => x.receipt_id);
  const offeredIntake = {};
  for (const scope of ["pending", "all", "authentic"]) {
    const g = buildPack(m, ["--scope", scope]);
    assert.equal(g.status, 0, `${scope}: ${g.all}`);
    offeredIntake[scope] = offered(m).filter((id) => intakeIds.includes(id));
  }
  assert.deepEqual(offeredIntake.pending, [A, B]);
  assert.deepEqual(offeredIntake.all, [A, B]);
  assert.deepEqual(offeredIntake.authentic, [], "intake receipts are outside the default 'authentic' scope by design");
  const before = treeState(m.root);
  for (const id of intakeIds.filter((x) => !offeredIntake.all.includes(x))) {
    const r = applyExports(m.root, [exportFile("REV-001", { [id]: goodEntry() })]);
    assert.equal(r.status, 1, `${id} (not offered) must be refused: ${r.all}`);
    assert.ok(r.err.includes(`receipt ${id}:`), r.err);
    assert.doesNotMatch(r.all, NO_CRASH);
  }
  assert.deepEqual(treeState(m.root), before, "all refusals together wrote nothing");
  for (const id of offeredIntake.all) assert.equal(applyExports(m.root, [exportFile("REV-001", { [id]: goodEntry() })]).status, 0, `${id} (offered) must be accepted`);
  assert.deepEqual(Object.keys(readJson(m.verifiedFile).records).sort(), [A, B]);
});

test("a register written by the REAL writer carries a hash the integrity check accepts (offered, accepted); changing the stored image afterwards makes both refuse it", () => {
  const m = fresh();
  const prov = { source_description: "synthetic fixture", source_type: "authentic_issued_receipt", owner: "TEST OWNER", permission_basis: "test", authorized_by: "TEST AUTHORIZER", retrieval_date: "2026-10-22", brand_family: "Nayara", receipt_family: "synthetic_nayara_a", personal_data_handling: "none", permitted_usage: { store: true, modify: false, evaluate: true, redistribute: false } };
  const img = path.join(m.root, "in", "real-writer.png"), pj = path.join(m.root, "in", "real-writer.json");
  fs.writeFileSync(img, Buffer.concat([PNG_1X1, Buffer.from("distinct-real-writer-bytes")]));
  fs.writeFileSync(pj, JSON.stringify(prov));
  for (const p of [img, pj]) assert.ok(path.resolve(p).startsWith(path.resolve(m.root) + path.sep));
  const w = spawnSync("node", [path.join(REAL_FUEL, "scripts", "register_intake.mjs"), img, "--provenance", pj, "--split", "development", "--fuel-dir", m.root], { encoding: "utf8" });
  assert.equal(w.status, 0, w.stdout + w.stderr);
  const entry = readJson(path.join(m.root, "intake", "intake_register.json")).receipts[0];
  assert.match(entry.sha256, /^[0-9a-f]{64}$/);
  assert.equal(entry.sha256, sha256Hex(fs.readFileSync(img)), "the writer hashes the raw source bytes");
  assert.equal(entry.sha256, sha256Hex(fs.readFileSync(path.join(m.root, entry.image_path))), "the stored copy is byte-identical to what was hashed");
  assert.equal(IR.intakeImageIntegrityProblem(m.root, entry), null);
  assert.equal(buildPack(m, ["--scope", "pending"]).status, 0);
  assert.deepEqual(offered(m), [entry.receipt_id]);
  fs.appendFileSync(path.join(m.root, entry.image_path), "tamper");
  const before = treeState(m.root);
  const r = applyExports(m.root, [exportFile("REV-001", { [entry.receipt_id]: goodEntry() })]);
  assert.equal(r.status, 1, r.all);
  assert.ok(r.err.includes(`receipt ${entry.receipt_id}: image integrity check failed`));
  assert.deepEqual(treeState(m.root), before);
  buildPack(m, ["--scope", "pending"]);
  assert.deepEqual(offered(m), [], "a tampered image is no longer offered");
});
