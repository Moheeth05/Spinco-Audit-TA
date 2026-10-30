// Cross-entry structural validation of the intake register: duplicate image paths, duplicate image hashes, and receipt families that sit on both splits.
// Schema basis (scripts/register_intake.mjs, intake/INTAKE.md): `receipt_family` is a REQUIRED provenance field copied onto every entry and compared by EXACT string
// equality when the writer enforces "a family goes wholly to one side"; `split` is exactly "development" or "heldout_untouched". Nothing here guesses a family from an
// id prefix or a name. Conflicts are rejected conservatively: EVERY entry in a conflicting group is unusable, so no member can become eligible (and no member
// can "win" by order). Structural validation stays pure (no filesystem). Synthetic only: every test builds a mirror (tests/helpers/syntheticMirror.mjs).
import test from "node:test";
import assert from "node:assert/strict";
import fs from "node:fs";
import path from "node:path";
import { spawnSync } from "node:child_process";
import { pathToFileURL } from "node:url";
import { makeMirror, assertIsolated, applyExports, exportFile, goodEntry, readJson, writeRegister, buildPack, treeState, intakeEntry, placeholderImageBytes, REAL_FUEL } from "./helpers/syntheticMirror.mjs";
import * as IR from "../src/intakeRegister.mjs";

const NO_CRASH = /TypeError|SyntaxError|ReferenceError|RangeError|at .*\.mjs:\d+/;
const regOf = (...receipts) => ({ note: "SYNTHETIC TEST FIXTURE", receipts });
const fresh = () => { const m = makeMirror(); assertIsolated(m.root); return m; };
const labelsOf = (root) => import(pathToFileURL(path.join(root, "src", "labels.mjs")).href + `?t=${Math.random()}`);
async function load(m) {
  const L = await labelsOf(m.root), warnings = [], orig = console.warn;
  console.warn = (...a) => warnings.push(a.join(" "));
  try { return { ...L.loadAnnotations(m.root), warnings, L }; } finally { console.warn = orig; }
}
const manifestOf = (m) => JSON.parse(fs.readFileSync(path.join(m.packDir, "manifest.js"), "utf8").replace(/^window\.REVIEW_MANIFEST = /, "").replace(/;\s*$/, ""));
const offered = (m) => manifestOf(m).items.map((x) => x.id).sort();
const intakeRecordIds = (records) => records.filter((r) => String(r.split).startsWith("intake_")).map((r) => r.receipt_id).sort();
const HELD = { split: "heldout_untouched" };

/** Run a register through the loader, the pack generator and the applier. `blocked` ids must be refused everywhere with the given reason; `usable` ids must work everywhere. */
async function check(label, entries, { blocked, usable = [], reason, omitImages = [] }) {
  const m = fresh();
  writeRegister(m, regOf(...entries), omitImages);
  const { records, summary } = await load(m);
  assert.deepEqual(intakeRecordIds(records), [...usable].sort(), `${label}: only the usable entries are loaded`);
  assert.equal(summary.skipped_intake_entries, blocked.length, `${label}: skipped count`);
  for (const id of blocked) assert.ok(summary.skipped_intake_detail.some((d) => d.receipt_id === id && reason.test(d.reason)), `${label}: ${id} skipped with a matching reason (${JSON.stringify(summary.skipped_intake_detail.filter((d) => d.receipt_id === id))})`);
  const g = buildPack(m, ["--scope", "all"]);
  assert.equal(g.status, 0, `${label}: ${g.all}`);
  assert.doesNotMatch(g.all, NO_CRASH);
  const o = offered(m).filter((id) => id.startsWith("SYNTH-INTAKE"));
  assert.deepEqual(o, [...usable].sort(), `${label}: the pack offers exactly the usable entries`);
  const before = treeState(m.root);
  for (const id of blocked) {
    const r = applyExports(m.root, [exportFile("REV-001", { [id]: goodEntry() })]);
    assert.equal(r.status, 1, `${label}: ${id} must be refused: ${r.all}`);
    assert.match(r.err, reason, `${label}: ${id}`);
    assert.doesNotMatch(r.all, NO_CRASH);
  }
  if (blocked.length && usable.length) {
    const mixed = applyExports(m.root, [exportFile("REV-001", { [usable[0]]: goodEntry(), [blocked[0]]: goodEntry() })]);
    assert.equal(mixed.status, 1, `${label}: one blocked id rejects the whole invocation`);
  }
  assert.deepEqual(treeState(m.root), before, `${label}: refusals wrote nothing`);
  for (const id of usable) assert.equal(applyExports(m.root, [exportFile("REV-001", { [id]: goodEntry() })]).status, 0, `${label}: ${id} must be accepted`);
  return m;
}

test("duplicate image_path: every entry sharing a path is unusable (even with different ids); a distinct neighbour is unaffected", async () => {
  const P = "corpus/authentic_intake/pending/shared.png";
  await check("exact duplicate path", [intakeEntry("SYNTH-INTAKE-A", { image_path: P }), intakeEntry("SYNTH-INTAKE-B", { image_path: P, receipt_family: "synthetic_family_b" }), intakeEntry("SYNTH-INTAKE-C")], { blocked: ["SYNTH-INTAKE-A", "SYNTH-INTAKE-B"], usable: ["SYNTH-INTAKE-C"], reason: /image_path appears on more than one register entry/ });
});

test("duplicate image_path is compared case-insensitively (NTFS paths are), so a case variant cannot slip through", async () => {
  await check("case-variant path", [intakeEntry("SYNTH-INTAKE-A", { image_path: "corpus/authentic_intake/pending/Same.png" }), intakeEntry("SYNTH-INTAKE-B", { image_path: "corpus/authentic_intake/pending/same.PNG" }), intakeEntry("SYNTH-INTAKE-C")], { blocked: ["SYNTH-INTAKE-A", "SYNTH-INTAKE-B"], usable: ["SYNTH-INTAKE-C"], reason: /image_path appears on more than one register entry/ });
});

test("duplicate valid sha256 (the same image registered twice under different ids and paths): every entry sharing the hash is unusable", async () => {
  const H = "a".repeat(64);
  await check("duplicate hash", [intakeEntry("SYNTH-INTAKE-A", { sha256: H }), intakeEntry("SYNTH-INTAKE-B", { sha256: H }), intakeEntry("SYNTH-INTAKE-X", { sha256: H, receipt_family: "synthetic_family_b" }), intakeEntry("SYNTH-INTAKE-C")], { blocked: ["SYNTH-INTAKE-A", "SYNTH-INTAKE-B", "SYNTH-INTAKE-X"], usable: ["SYNTH-INTAKE-C"], reason: /sha256 appears on more than one register entry/ });
});

test("hashes that are missing or malformed are NOT compared as duplicates (they are refused at the integrity boundary instead); an uppercase variant is not a 'duplicate' of the lowercase hash", async () => {
  const m = fresh();
  const same = placeholderImageBytes("SYNTH-INTAKE-OK"), good = intakeEntry("SYNTH-INTAKE-OK");
  const { sha256: _d, ...noHash1 } = intakeEntry("SYNTH-INTAKE-NOH1"); // eslint-disable-line no-unused-vars
  const { sha256: _e, ...noHash2 } = intakeEntry("SYNTH-INTAKE-NOH2"); // eslint-disable-line no-unused-vars
  writeRegister(m, regOf(good, intakeEntry("SYNTH-INTAKE-MAL1", { sha256: "zz" }), intakeEntry("SYNTH-INTAKE-MAL2", { sha256: "zz" }), noHash1, noHash2, intakeEntry("SYNTH-INTAKE-UP", { sha256: good.sha256.toUpperCase() })));
  assert.ok(same.length > 0);
  const { records, summary } = await load(m);
  assert.equal(summary.skipped_intake_entries, 0, "all six are structurally usable (the pure check only compares VALID hashes)");
  assert.equal(intakeRecordIds(records).length, 6);
  assert.equal(buildPack(m, ["--scope", "all"]).status, 0);
  assert.deepEqual(offered(m).filter((x) => x.startsWith("SYNTH-INTAKE")), ["SYNTH-INTAKE-OK"], "only the entry with a valid, matching hash is offered");
  for (const id of ["SYNTH-INTAKE-MAL1", "SYNTH-INTAKE-NOH1", "SYNTH-INTAKE-UP"]) {
    const r = applyExports(m.root, [exportFile("REV-001", { [id]: goodEntry() })]);
    assert.equal(r.status, 1, `${id}: ${r.all}`);
    assert.match(r.err, /sha256 is (missing|not 64 lowercase hex)/);
  }
});

test("a family on BOTH splits: every entry of that family (both sides) is unusable; other families and same-split families are unaffected", async () => {
  await check("cross-split family", [
    intakeEntry("SYNTH-INTAKE-F1", { receipt_family: "synthetic_fam_X" }), intakeEntry("SYNTH-INTAKE-F2", { receipt_family: "synthetic_fam_X" }),
    intakeEntry("SYNTH-INTAKE-F3", { receipt_family: "synthetic_fam_X", ...HELD }),
    intakeEntry("SYNTH-INTAKE-G1", { receipt_family: "synthetic_fam_Y" }), intakeEntry("SYNTH-INTAKE-G2", { receipt_family: "synthetic_fam_Y" }),
    intakeEntry("SYNTH-INTAKE-H1", { receipt_family: "synthetic_fam_Z", ...HELD }), intakeEntry("SYNTH-INTAKE-H2", { receipt_family: "synthetic_fam_Z", ...HELD }),
  ], { blocked: ["SYNTH-INTAKE-F1", "SYNTH-INTAKE-F2", "SYNTH-INTAKE-F3"], usable: ["SYNTH-INTAKE-G1", "SYNTH-INTAKE-G2", "SYNTH-INTAKE-H1", "SYNTH-INTAKE-H2"], reason: /receipt_family 'synthetic_fam_X' appears under both development and heldout_untouched/ });
});

test("conservative: an otherwise INVALID entry that declares the opposite split for a family still blocks the valid entries of that family", async () => {
  await check("invalid conflicting entry", [intakeEntry("SYNTH-INTAKE-A", { receipt_family: "synthetic_fam_X" }), intakeEntry("SYNTH-INTAKE-B", { receipt_family: "synthetic_fam_X", ...HELD, provenance: null }), intakeEntry("SYNTH-INTAKE-C")], { blocked: ["SYNTH-INTAKE-A", "SYNTH-INTAKE-B"], usable: ["SYNTH-INTAKE-C"], reason: /./ });
  const m = fresh();
  writeRegister(m, regOf(intakeEntry("SYNTH-INTAKE-A", { receipt_family: "synthetic_fam_X" }), intakeEntry("SYNTH-INTAKE-B", { receipt_family: "synthetic_fam_X", ...HELD, provenance: null })));
  const { summary } = await load(m);
  assert.match(summary.skipped_intake_detail.find((d) => d.receipt_id === "SYNTH-INTAKE-A").reason, /appears under both development and heldout_untouched/, "the VALID entry is blocked for the family conflict, not for anything of its own");
});

test("family identity is the writer's: EXACT string equality - spelling variants are distinct families (no guessing from names), same-family same-split entries are fine", async () => {
  const m = await check("distinct spellings", [intakeEntry("SYNTH-INTAKE-A", { receipt_family: "Synthetic_Fam" }), intakeEntry("SYNTH-INTAKE-B", { receipt_family: "synthetic_fam", ...HELD }), intakeEntry("SYNTH-INTAKE-C", { receipt_family: " synthetic_fam", ...HELD }), intakeEntry("SYNTH-INTAKE-D", { receipt_family: "synthetic_fam", ...HELD })], { blocked: [], usable: ["SYNTH-INTAKE-A", "SYNTH-INTAKE-B", "SYNTH-INTAKE-C", "SYNTH-INTAKE-D"], reason: /./ });
  assert.ok(m);
});

test("outcome does not depend on entry order (no entry 'wins' by being first)", async () => {
  const a = intakeEntry("SYNTH-INTAKE-A", { receipt_family: "synthetic_fam_X" }), b = intakeEntry("SYNTH-INTAKE-B", { receipt_family: "synthetic_fam_X", ...HELD }), c = intakeEntry("SYNTH-INTAKE-C"), d = intakeEntry("SYNTH-INTAKE-D", { sha256: c.sha256 });
  const classify = (list) => { const r = IR.inspectIntakeEntries(list); return [r.usable.map((e) => e.receipt_id).sort(), r.rejected.map((x) => x.receipt_id).sort()]; };
  const base = classify([a, b, c, d]);
  assert.deepEqual(base[0], [], "A/B (family) and C/D (hash) all conflict");
  for (const perm of [[d, c, b, a], [b, d, a, c], [c, a, d, b]]) assert.deepEqual(classify(perm), base);
});

test("missing / blank / non-text receipt_family and a missing / unknown split make the entry unusable (the family rule could not be evaluated; the loader no longer defaults an unknown split to development)", async () => {
  const bad = [["family missing", { receipt_family: undefined }, /receipt_family is missing, blank or not text/], ["family null", { receipt_family: null }, /receipt_family/], ["family blank", { receipt_family: "" }, /receipt_family/], ["family spaces", { receipt_family: "   " }, /receipt_family/], ["family number", { receipt_family: 7 }, /receipt_family/], ["family list", { receipt_family: ["a"] }, /receipt_family/],
    ["split missing", { split: undefined }, /split is missing or not one of/], ["split null", { split: null }, /split/], ["split typo", { split: "dev" }, /split/], ["split case", { split: "HELDOUT_UNTOUCHED" }, /split/], ["split padded", { split: "development " }, /split/], ["split alias", { split: "heldout" }, /split/], ["split number", { split: 5 }, /split/]];
  for (const [label, o, reason] of bad) await check(label, [intakeEntry("SYNTH-INTAKE-BAD", o), intakeEntry("SYNTH-INTAKE-OK")], { blocked: ["SYNTH-INTAKE-BAD"], usable: ["SYNTH-INTAKE-OK"], reason });
});

test("structural validation is pure: the conflicts are found from in-memory entries alone, with no directory and no files", () => {
  const entries = [intakeEntry("A1", { image_path: "corpus/authentic_intake/pending/p.png" }), intakeEntry("A2", { image_path: "corpus/authentic_intake/pending/P.png" }), intakeEntry("B1", { sha256: "b".repeat(64) }), intakeEntry("B2", { sha256: "b".repeat(64) }), intakeEntry("C1", { receipt_family: "fam" }), intakeEntry("C2", { receipt_family: "fam", ...HELD }), intakeEntry("OK")];
  const r = IR.inspectIntakeEntries(entries);
  assert.deepEqual(r.usable.map((e) => e.receipt_id), ["OK"]);
  assert.equal(r.rejected.length, 6);
  assert.deepEqual(IR.entryProblems(intakeEntry("OK")), []);
  assert.deepEqual(IR.entryProblems(intakeEntry("OK", HELD)), []);
  assert.equal(IR.inspectIntakeEntries.length <= 2, true, "no directory parameter");
});

test("compatibility: a normal register (unique paths, hashes and families, both splits, several receipts per family) is fully usable and works end to end", async () => {
  const entries = [intakeEntry("SYNTH-INTAKE-A", { receipt_family: "synthetic_fam_1" }), intakeEntry("SYNTH-INTAKE-B", { receipt_family: "synthetic_fam_1" }), intakeEntry("SYNTH-INTAKE-C", { receipt_family: "synthetic_fam_2", ...HELD }), intakeEntry("SYNTH-INTAKE-D", { receipt_family: "synthetic_fam_2", ...HELD }), intakeEntry("SYNTH-INTAKE-E", { receipt_family: "synthetic_fam_3" })];
  const m = await check("normal register", entries, { blocked: [], usable: entries.map((e) => e.receipt_id), reason: /./ });
  assert.deepEqual(Object.keys(readJson(m.verifiedFile).records).sort(), entries.map((e) => e.receipt_id).sort());
});

test("compatibility with the REAL writer: a register it produced is usable; the writer itself refuses a cross-split family (same rule); a hand-added conflicting entry then blocks the whole family", async () => {
  const m = fresh();
  const PNG = placeholderImageBytes("real-writer-base");
  const prov = (family) => ({ source_description: "synthetic fixture", source_type: "authentic_issued_receipt", owner: "TEST OWNER", permission_basis: "test", authorized_by: "TEST AUTHORIZER", retrieval_date: "2026-10-22", brand_family: "Nayara", receipt_family: family, personal_data_handling: "none", permitted_usage: { store: true, modify: false, evaluate: true, redistribute: false } });
  let n = 0;
  const register = (family, split) => {
    const img = path.join(m.root, "in", `rw-${++n}.png`), pj = path.join(m.root, "in", `rw-${n}.json`);
    fs.writeFileSync(img, Buffer.concat([PNG, Buffer.from(`distinct-${n}`)]));
    fs.writeFileSync(pj, JSON.stringify(prov(family)));
    for (const p of [img, pj]) assert.ok(path.resolve(p).startsWith(path.resolve(m.root) + path.sep));
    return spawnSync("node", [path.join(REAL_FUEL, "scripts", "register_intake.mjs"), img, "--provenance", pj, "--split", split, "--fuel-dir", m.root], { encoding: "utf8" });
  };
  assert.equal(register("synthetic_rw_X", "development").status, 0);
  assert.equal(register("synthetic_rw_X", "development").status, 0, "same family, same split: allowed");
  assert.equal(register("synthetic_rw_Y", "heldout_untouched").status, 0);
  const refused = register("synthetic_rw_X", "heldout_untouched");
  assert.equal(refused.status, 1, "the writer refuses a family that is already on the other side");
  assert.match(refused.stderr, /goes wholly to one side/);
  const regFile = path.join(m.root, "intake", "intake_register.json");
  const reg = readJson(regFile);
  assert.equal(reg.receipts.length, 3);
  let r = await load(m);
  assert.equal(r.summary.skipped_intake_entries, 0, "everything the real writer produced is usable");
  assert.equal(intakeRecordIds(r.records).length, 3);
  // someone edits the register by hand to put the family on both sides
  reg.receipts.push({ ...reg.receipts[2], receipt_id: "intake_nayara_999", split: "heldout_untouched", receipt_family: "synthetic_rw_X", sha256: "c".repeat(64), image_path: "corpus/authentic_intake/pending/intake_nayara_999.png" });
  fs.writeFileSync(regFile, JSON.stringify(reg));
  r = await load(m);
  assert.equal(r.summary.skipped_intake_entries, 3, "both development entries of family X and the injected held-out one are blocked");
  assert.deepEqual(intakeRecordIds(r.records), [reg.receipts[2].receipt_id], "family Y is untouched");
});

test("pack and applier agree on a register containing every conflict kind next to valid entries (scopes pending, all, authentic): offered <=> accepted, everything else refused, nothing written", async () => {
  const P = "corpus/authentic_intake/pending/shared.png", H = "d".repeat(64);
  const entries = [
    intakeEntry("SYNTH-INTAKE-OK1"), intakeEntry("SYNTH-INTAKE-OK2", { receipt_family: "synthetic_fam_2", ...HELD }),
    intakeEntry("SYNTH-INTAKE-P1", { image_path: P }), intakeEntry("SYNTH-INTAKE-P2", { image_path: P }),
    intakeEntry("SYNTH-INTAKE-H1", { sha256: H }), intakeEntry("SYNTH-INTAKE-H2", { sha256: H }),
    intakeEntry("SYNTH-INTAKE-X1", { receipt_family: "synthetic_fam_X" }), intakeEntry("SYNTH-INTAKE-X2", { receipt_family: "synthetic_fam_X", ...HELD }),
    intakeEntry("SYNTH-INTAKE-NOSPLIT", { split: "dev" }),
  ];
  const m = fresh();
  writeRegister(m, regOf(...entries));
  const intakeIds = entries.map((e) => e.receipt_id);
  const per = {};
  for (const scope of ["pending", "all", "authentic"]) { assert.equal(buildPack(m, ["--scope", scope]).status, 0); per[scope] = offered(m).filter((id) => intakeIds.includes(id)); }
  assert.deepEqual(per.pending, ["SYNTH-INTAKE-OK1", "SYNTH-INTAKE-OK2"]);
  assert.deepEqual(per.all, ["SYNTH-INTAKE-OK1", "SYNTH-INTAKE-OK2"]);
  assert.deepEqual(per.authentic, [], "intake receipts are outside the default scope by design");
  const before = treeState(m.root);
  for (const id of intakeIds.filter((x) => !per.all.includes(x))) assert.equal(applyExports(m.root, [exportFile("REV-001", { [id]: goodEntry() })]).status, 1, `${id} must be refused`);
  assert.deepEqual(treeState(m.root), before);
  for (const id of per.all) assert.equal(applyExports(m.root, [exportFile("REV-001", { [id]: goodEntry() })]).status, 0, `${id} must be accepted`);
});
