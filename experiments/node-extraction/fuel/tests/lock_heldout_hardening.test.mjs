// Held-out lock hardening (scripts/lock_heldout.mjs). A lock may only be created from a structurally valid register whose HELD-OUT entries' images are available and
// match their recorded SHA-256; every check runs before anything is written; an existing lock is never overwritten, repaired or deleted; an empty lock (no eligible held-out
// entry, no register) is refused (owner decision). Decisions made where the contracts were silent, all fail-closed: any structurally unusable entry anywhere in the register
// blocks locking (the whole register must be valid); images are checked only for the held-out entries that go into the lock.
// Synthetic only: every test runs the mirror's COPY of the script (it derives its data directory from its own location, so it can only touch the mirror) against invented ids
// and distinct placeholder images (tests/helpers/syntheticMirror.mjs).
import test from "node:test";
import assert from "node:assert/strict";
import fs from "node:fs";
import path from "node:path";
import { spawnSync } from "node:child_process";
import { makeMirror, assertIsolated, runScript, writeRegister, writeRegisterRaw, treeState, intakeEntry, placeholderImageBytes, readJson, REAL_FUEL, COPIED } from "./helpers/syntheticMirror.mjs";
import { hashCode, lockStatus, CODE_FILES } from "../src/codeHash.mjs";

const HELD = { split: "heldout_untouched" };
const NO_CRASH = /TypeError|SyntaxError|ReferenceError|RangeError|at .*\.mjs:\d+/;
const STATEMENT = "From this moment any change to the files below makes the listed receipts DEVELOPMENT data, not an untouched held-out set.";
const regOf = (...receipts) => ({ note: "SYNTHETIC TEST FIXTURE", receipts });
const fresh = () => { const m = makeMirror(); assertIsolated(m.root); return m; };
const lockFile = (m) => path.join(m.root, "intake", "heldout_lock.json");
const lock = (m) => runScript(m.root, "lock_heldout.mjs");
const imgAbs = (m, id) => path.join(m.root, "corpus", "authentic_intake", "pending", `${id}.png`);

/** The lock command must refuse: non-zero exit, a REFUSED message with every expected fragment, no lock, and a mirror that is byte-identical afterwards. */
function assertRefused(label, m, before, fragments = []) {
  const r = lock(m);
  assert.equal(r.status, 1, `${label}: must exit 1 - ${r.all}`);
  assert.match(r.err, /REFUSED - no lock was written/, label);
  for (const f of fragments) assert.ok(r.err.includes(f), `${label}: expected "${f}" in:\n${r.err}`);
  assert.doesNotMatch(r.all, NO_CRASH, label);
  assert.ok(!fs.existsSync(lockFile(m)), `${label}: no lock may exist`);
  assert.deepEqual(treeState(m.root), before, `${label}: every protected mirror file must be unchanged (nothing written, no temp file)`);
  return r;
}

test("the mirror's stand-in code files match the real CODE_FILES list, and the copied script is the real one", () => {
  const m = fresh();
  for (const f of CODE_FILES) assert.ok(fs.existsSync(path.join(m.root, f)), f);
  assert.ok(COPIED.includes("scripts/lock_heldout.mjs") && COPIED.includes("src/codeHash.mjs"));
  assert.equal(fs.readFileSync(path.join(m.root, "scripts", "lock_heldout.mjs"), "utf8"), fs.readFileSync(path.join(REAL_FUEL, "scripts", "lock_heldout.mjs"), "utf8"));
  assert.ok(!fs.existsSync(lockFile(m)));
});

test("1. a valid register produces the expected lock: unchanged schema and statement, only held-out entries, hashes from the register, code hashes of the mirror's code", () => {
  const m = fresh();
  const entries = [intakeEntry("SYNTH-INTAKE-H1", { receipt_family: "synthetic_fam_H", brand_family: "Nayara", ...HELD }), intakeEntry("SYNTH-INTAKE-H2", { receipt_family: "synthetic_fam_H", ...HELD }), intakeEntry("SYNTH-INTAKE-D1", { receipt_family: "synthetic_fam_D" })];
  writeRegister(m, regOf(...entries));
  const r = lock(m);
  assert.equal(r.status, 0, r.all);
  assert.match(r.out, /^locked 2 held-out intake receipt\(s\) to the current extractor/);
  const l = readJson(lockFile(m));
  assert.deepEqual(Object.keys(l), ["locked_at", "statement", "code_sha256", "heldout_receipts"], "the lock schema is unchanged");
  assert.equal(l.statement, STATEMENT);
  assert.ok(!Number.isNaN(Date.parse(l.locked_at)));
  assert.deepEqual(l.code_sha256, hashCode(m.root));
  assert.deepEqual(l.heldout_receipts, [
    { id: "SYNTH-INTAKE-H1", family: "synthetic_fam_H", brand: "Nayara", sha256: entries[0].sha256 },
    { id: "SYNTH-INTAKE-H2", family: "synthetic_fam_H", brand: "other", sha256: entries[1].sha256 },
  ]);
  assert.deepEqual({ ...lockStatus(m.root), locked_at: undefined }, { locked: true, locked_at: undefined, still_untouched: true, changed_files: [], receipts: 2 });
  fs.appendFileSync(path.join(m.root, "src", "fuelExtractor.mjs"), "// a parser change after the lock\n"); // the existing code-integrity check still works
  assert.deepEqual(lockStatus(m.root).changed_files, ["src/fuelExtractor.mjs"]);
});

test("10. an unrelated DEVELOPMENT receipt never enters the lock - and its own image is not required (only held-out images are checked)", () => {
  const m = fresh();
  writeRegister(m, regOf(intakeEntry("SYNTH-INTAKE-H1", { receipt_family: "synthetic_fam_H", ...HELD }), intakeEntry("SYNTH-INTAKE-D1", { receipt_family: "synthetic_fam_D" }), intakeEntry("SYNTH-INTAKE-D2", { receipt_family: "synthetic_fam_D", sha256: "e".repeat(64) })), ["SYNTH-INTAKE-D1"]);
  assert.ok(!fs.existsSync(imgAbs(m, "SYNTH-INTAKE-D1")), "D1 has no image; D2's recorded hash does not match its image");
  const r = lock(m);
  assert.equal(r.status, 0, r.all);
  assert.deepEqual(readJson(lockFile(m)).heldout_receipts.map((x) => x.id), ["SYNTH-INTAKE-H1"]);
  assert.match(r.out, /locked 1 held-out/);
});

test("2.-5. a held-out image that is missing, empty, modified (hash mismatch), or has a missing / malformed hash prevents the lock; nothing is written", () => {
  const { sha256: _d, ...noHash } = intakeEntry("SYNTH-INTAKE-BAD", HELD); // eslint-disable-line no-unused-vars
  const cases = [
    ["missing image", intakeEntry("SYNTH-INTAKE-BAD", HELD), { omit: true }, "image file not found"],
    ["empty image", intakeEntry("SYNTH-INTAKE-BAD", HELD), { empty: true }, "image unavailable (empty file)"],
    ["modified image", intakeEntry("SYNTH-INTAKE-BAD", HELD), { modify: true }, "image integrity check failed"],
    ["wrong but well-formed hash", intakeEntry("SYNTH-INTAKE-BAD", { sha256: "5".repeat(64), ...HELD }), {}, "image integrity check failed"],
    ["missing hash", noHash, {}, "sha256 is missing"],
    ["null hash", intakeEntry("SYNTH-INTAKE-BAD", { sha256: null, ...HELD }), {}, "sha256 is missing"],
    ["malformed hash", intakeEntry("SYNTH-INTAKE-BAD", { sha256: "not-a-hash", ...HELD }), {}, "sha256 is not 64 lowercase hex"],
    ["uppercase hash", intakeEntry("SYNTH-INTAKE-BAD", { sha256: intakeEntry("SYNTH-INTAKE-BAD").sha256.toUpperCase(), ...HELD }), {}, "sha256 is not 64 lowercase hex"],
  ];
  for (const [label, entry, how, fragment] of cases) {
    const m = fresh();
    writeRegister(m, regOf(intakeEntry("SYNTH-INTAKE-GOOD", { receipt_family: "synthetic_fam_G", ...HELD }), entry), how.omit ? ["SYNTH-INTAKE-BAD"] : []);
    if (how.empty) fs.writeFileSync(imgAbs(m, "SYNTH-INTAKE-BAD"), "");
    if (how.modify) fs.appendFileSync(imgAbs(m, "SYNTH-INTAKE-BAD"), "-modified-after-registration");
    assertRefused(label, m, treeState(m.root), ["SYNTH-INTAKE-BAD", fragment]);
  }
});

test("6. an invalid register (no file, unreadable, not an object, receipts missing / not a list) prevents the lock - none of them crash", () => {
  const m0 = fresh();
  assertRefused("no register file", m0, treeState(m0.root), ["no intake register"]);
  for (const text of ["{ not json", "", "[]", "null", '"s"', "5", "{}", '{"receipts":null}', '{"receipts":{}}', '{"receipts":"x"}']) {
    const m = fresh();
    writeRegisterRaw(m, text);
    assertRefused(`register ${JSON.stringify(text)}`, m, treeState(m.root), ["intake register is invalid"]);
  }
});

test("owner decision: no eligible held-out entry (empty list, or development entries only) is refused - an empty lock is never created", () => {
  const m = fresh();
  writeRegister(m, regOf());
  assertRefused("empty receipts list", m, treeState(m.root), ["no eligible held-out"]);
  const m2 = fresh();
  writeRegister(m2, regOf(intakeEntry("SYNTH-INTAKE-D1"), intakeEntry("SYNTH-INTAKE-D2", { receipt_family: "synthetic_fam_D2" })));
  assertRefused("development entries only", m2, treeState(m2.root), ["no eligible held-out"]);
});

test("7. duplicate image path or duplicate hash prevents the lock (also case variants; also when the duplicates are development entries - the whole register must be valid)", () => {
  const P = "corpus/authentic_intake/pending/shared.png";
  const sets = [
    ["duplicate path (held-out pair)", [intakeEntry("SYNTH-INTAKE-H1", { image_path: P, ...HELD }), intakeEntry("SYNTH-INTAKE-H2", { image_path: P, receipt_family: "synthetic_fam_2", ...HELD })], "image_path appears on more than one"],
    ["case-variant path", [intakeEntry("SYNTH-INTAKE-H1", { image_path: "corpus/authentic_intake/pending/Same.png", ...HELD }), intakeEntry("SYNTH-INTAKE-H2", { image_path: "corpus/authentic_intake/pending/same.png", receipt_family: "synthetic_fam_2", ...HELD })], "image_path appears on more than one"],
    ["duplicate hash (held-out pair)", [intakeEntry("SYNTH-INTAKE-H1", { sha256: "a".repeat(64), ...HELD }), intakeEntry("SYNTH-INTAKE-H2", { sha256: "a".repeat(64), receipt_family: "synthetic_fam_2", ...HELD })], "sha256 appears on more than one"],
    ["duplicate hash across a development and a held-out entry", [intakeEntry("SYNTH-INTAKE-H1", { sha256: "b".repeat(64), ...HELD }), intakeEntry("SYNTH-INTAKE-D1", { sha256: "b".repeat(64), receipt_family: "synthetic_fam_D" })], "sha256 appears on more than one"],
    ["duplicate between two development entries only", [intakeEntry("SYNTH-INTAKE-H1", HELD), intakeEntry("SYNTH-INTAKE-D1", { image_path: P, receipt_family: "synthetic_fam_D" }), intakeEntry("SYNTH-INTAKE-D2", { image_path: P, receipt_family: "synthetic_fam_D" })], "image_path appears on more than one"],
    ["duplicate receipt_id", [intakeEntry("SYNTH-INTAKE-H1", HELD), intakeEntry("SYNTH-INTAKE-H1", { receipt_family: "synthetic_fam_2", ...HELD })], "receipt_id appears more than once"],
  ];
  for (const [label, entries, fragment] of sets) {
    const m = fresh();
    writeRegister(m, regOf(...entries));
    assertRefused(label, m, treeState(m.root), [fragment]);
  }
});

test("8. a receipt family on BOTH splits prevents the lock", () => {
  const m = fresh();
  writeRegister(m, regOf(intakeEntry("SYNTH-INTAKE-H1", { receipt_family: "synthetic_fam_X", ...HELD }), intakeEntry("SYNTH-INTAKE-D1", { receipt_family: "synthetic_fam_X" }), intakeEntry("SYNTH-INTAKE-H2", { receipt_family: "synthetic_fam_Y", ...HELD })));
  assertRefused("cross-split family", m, treeState(m.root), ["receipt_family 'synthetic_fam_X' appears under both development and heldout_untouched", "SYNTH-INTAKE-H1", "SYNTH-INTAKE-D1"]);
});

test("any structurally unusable entry blocks the lock (decision: the whole register must be valid), however unrelated: bad provenance, bad split, collision with an annotation id, non-object", () => {
  const sets = [
    ["unusable development entry (no provenance)", [intakeEntry("SYNTH-INTAKE-H1", HELD), intakeEntry("SYNTH-INTAKE-D1", { receipt_family: "synthetic_fam_D", provenance: null })], "provenance is missing or malformed"],
    ["entry with an unknown split", [intakeEntry("SYNTH-INTAKE-H1", HELD), intakeEntry("SYNTH-INTAKE-D1", { receipt_family: "synthetic_fam_D", split: "dev" })], "split is missing or not one of"],
    ["id colliding with an annotation record", [intakeEntry("SYNTH-INTAKE-H1", HELD), intakeEntry("SYNTH-AUTH-001", { receipt_family: "synthetic_fam_D" })], "collides with a record in corpus/annotations.json"],
    ["non-object entry", [intakeEntry("SYNTH-INTAKE-H1", HELD), null], "entry is not an object"],
  ];
  for (const [label, entries, fragment] of sets) {
    const m = fresh();
    writeRegister(m, regOf(...entries));
    assertRefused(label, m, treeState(m.root), [fragment]);
  }
});

test("9. an existing lock is preserved byte-for-byte and the command exits non-zero - even for a valid register, after a code change, and for a corrupt, empty or directory 'lock'", () => {
  const m = fresh();
  writeRegister(m, regOf(intakeEntry("SYNTH-INTAKE-H1", HELD)));
  assert.equal(lock(m).status, 0);
  const original = fs.readFileSync(lockFile(m));
  const before = treeState(m.root);
  fs.appendFileSync(path.join(m.root, "src", "fuelExtractor.mjs"), "// parser changed after the lock\n");
  const afterCodeChange = treeState(m.root);
  const r = lock(m);
  assert.equal(r.status, 1, r.all);
  assert.match(r.err, /REFUSED - no lock was written/);
  assert.match(r.err, /heldout_lock\.json/);
  assert.match(r.err, /never overwritten, repaired or deleted/);
  assert.deepEqual(fs.readFileSync(lockFile(m)), original, "the lock is byte-identical");
  assert.deepEqual(treeState(m.root), afterCodeChange, "nothing else changed either");
  assert.notDeepEqual(afterCodeChange, before);
  for (const label of ["corrupt", "empty", "directory"]) {
    const m2 = fresh();
    writeRegister(m2, regOf(intakeEntry("SYNTH-INTAKE-H1", HELD)));
    fs.mkdirSync(path.join(m2.root, "intake"), { recursive: true });
    if (label === "directory") fs.mkdirSync(lockFile(m2)); else fs.writeFileSync(lockFile(m2), label === "corrupt" ? "{ corrupt" : "");
    const snap = treeState(m2.root);
    const r2 = lock(m2);
    assert.equal(r2.status, 1, `${label}: ${r2.all}`);
    assert.match(r2.err, /never overwritten, repaired or deleted/);
    assert.deepEqual(treeState(m2.root), snap, `${label}: untouched`);
  }
});

test("a refusal because of an existing lock takes precedence and reports only that (no register work, nothing else touched)", () => {
  const m = fresh();
  writeRegisterRaw(m, "{ not json");
  fs.mkdirSync(path.join(m.root, "intake"), { recursive: true });
  fs.writeFileSync(lockFile(m), JSON.stringify({ sentinel: true }));
  const before = treeState(m.root);
  const r = lock(m);
  assert.equal(r.status, 1);
  assert.match(r.err, /never overwritten, repaired or deleted/);
  assert.deepEqual(treeState(m.root), before);
});

test("12. a register produced by the REAL writer (scripts/register_intake.mjs, pointed at the mirror) is locked correctly: only its held-out receipts, writer hashes; a second run is refused", () => {
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
  register("synthetic_rw_dev", "development"); register("synthetic_rw_held", "heldout_untouched"); register("synthetic_rw_held", "heldout_untouched");
  const reg = readJson(path.join(m.root, "intake", "intake_register.json")).receipts;
  const r = lock(m);
  assert.equal(r.status, 0, r.all);
  const l = readJson(lockFile(m));
  assert.deepEqual(l.heldout_receipts, reg.filter((e) => e.split === "heldout_untouched").map((e) => ({ id: e.receipt_id, family: e.receipt_family, brand: e.brand_family, sha256: e.sha256 })));
  assert.equal(l.heldout_receipts.length, 2);
  const bytes = fs.readFileSync(lockFile(m));
  const again = lock(m);
  assert.equal(again.status, 1);
  assert.deepEqual(fs.readFileSync(lockFile(m)), bytes, "the second run left the lock byte-identical");
});

test("a held-out image tampered with after registration blocks locking until the original bytes are restored", () => {
  const m = fresh();
  writeRegister(m, regOf(intakeEntry("SYNTH-INTAKE-H1", HELD)));
  fs.appendFileSync(imgAbs(m, "SYNTH-INTAKE-H1"), "tamper");
  assertRefused("tampered held-out image", m, treeState(m.root), ["SYNTH-INTAKE-H1", "image integrity check failed"]);
  fs.writeFileSync(imgAbs(m, "SYNTH-INTAKE-H1"), placeholderImageBytes("SYNTH-INTAKE-H1"));
  assert.equal(lock(m).status, 0);
});

test("all problems are reported together (one pass, not one at a time) and the output never contains image content", () => {
  const m = fresh();
  writeRegister(m, regOf(intakeEntry("SYNTH-INTAKE-H1", { sha256: "6".repeat(64), ...HELD }), intakeEntry("SYNTH-INTAKE-H2", { receipt_family: "synthetic_fam_2", ...HELD }), intakeEntry("SYNTH-INTAKE-H3", { receipt_family: "synthetic_fam_3", ...HELD })), ["SYNTH-INTAKE-H2"]);
  const r = assertRefused("several problems", m, treeState(m.root), ["receipt SYNTH-INTAKE-H1: image integrity check failed", "receipt SYNTH-INTAKE-H2: image file not found"]);
  assert.ok(!r.err.includes("receipt SYNTH-INTAKE-H3"), "the intact held-out receipt is not blamed");
  assert.ok(!/[\u0000-\u0008\u000e-\u001f]/.test(r.all), "no raw image bytes in the output");
});
