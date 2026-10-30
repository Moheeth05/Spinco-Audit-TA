// Malformed intake-register handling across the loader (src/labels.mjs), the review-pack generator and apply_verification.mjs.
// Contract (src/intakeRegister.mjs is the single definition): a register entry is USABLE only if it is an object with a non-blank text receipt_id, a plausible
// image_path inside corpus/authentic_intake/, provenance that proves an authentic issued receipt, an id unique in the register and not colliding with an
// annotation id. An unusable register (unreadable / not an object / `receipts` missing or not a list) is never trusted: the loader loads no intake receipt,
// the generator writes no pack, the applier rejects the whole invocation before any write. The pack offers only what the applier would accept.
// Synthetic only: every test builds a mirror (tests/helpers/syntheticMirror.mjs) with invented ids; the register is written AFTER assertIsolated, inside the mirror.
import test from "node:test";
import assert from "node:assert/strict";
import fs from "node:fs";
import path from "node:path";
import { spawnSync } from "node:child_process";
import { pathToFileURL } from "node:url";
import crypto from "node:crypto";
import { makeMirror, assertIsolated, applyExports, exportFile, goodEntry, readJson, writeRegister, writeRegisterRaw, buildPack, placeIntakeImages, REAL_FUEL, placeholderSha256 } from "./helpers/syntheticMirror.mjs";
import * as IR from "../src/intakeRegister.mjs"; // pure code + file stats inside synthetic mirrors only

const NO_CRASH = /TypeError|SyntaxError|ReferenceError|RangeError|at .*\.mjs:\d+/;
const validEntry = (id, o = {}) => ({
  receipt_id: id, image_path: `corpus/authentic_intake/pending/${id}.png`, sha256: placeholderSha256(id), registered_at: "2026-10-22T00:00:00Z", split: "development",
  receipt_family: "synthetic_family_a", brand_family: "other", label_status: "pending_human_label",
  provenance: { source_type: "authentic_issued_receipt", source_description: "synthetic fixture description" }, ...o,
});
const regOf = (...receipts) => ({ note: "SYNTHETIC TEST FIXTURE", receipts });
const labelsOf = (root) => import(pathToFileURL(path.join(root, "src", "labels.mjs")).href + `?t=${Math.random()}`);
async function load(m) {
  const L = await labelsOf(m.root), warnings = [], orig = console.warn;
  console.warn = (...a) => warnings.push(a.join(" "));
  try { return { ...L.loadAnnotations(m.root), warnings, L }; } finally { console.warn = orig; }
}
const ids = (records) => records.map((r) => r.receipt_id);
const manifestOf = (m) => JSON.parse(fs.readFileSync(path.join(m.packDir, "manifest.js"), "utf8").replace(/^window\.REVIEW_MANIFEST = /, "").replace(/;\s*$/, ""));
const offered = (m) => manifestOf(m).items.map((x) => x.id).sort();
const fresh = () => { const m = makeMirror(); assertIsolated(m.root); return m; };
const annotationIds = ["SYNTH-AUTH-001", "SYNTH-AUTH-002", "SYNTH-CTRL-001", "SYNTH-FUEL-001", "SYNTH-TPL-001"];

test("no register file: status 'absent', nothing skipped, no warnings; an empty list is a valid register", async () => {
  const m = fresh();
  let r = await load(m);
  assert.equal(r.summary.intake_register_status, "absent");
  assert.equal(r.summary.skipped_intake_entries, 0);
  assert.deepEqual(r.warnings, []);
  assert.equal(r.records.length, 5);
  writeRegister(m, regOf());
  r = await load(m);
  assert.equal(r.summary.intake_register_status, "ok");
  assert.deepEqual(r.warnings, []);
});

test("an UNUSABLE register (bad JSON / not an object / receipts missing or not a list): loader loads no intake receipt, generator writes no pack, applier rejects before any write - none of them crash", async () => {
  const bad = ["{ not json", "", "[]", "null", '"a string"', "5", "{}", '{"receipts":null}', '{"receipts":{}}', '{"receipts":"x"}', '{"receipts":5}', '{"note":"no receipts key"}'];
  for (const text of bad) {
    const m = fresh();
    writeRegisterRaw(m, text);
    const { records, summary, warnings } = await load(m);
    assert.equal(summary.intake_register_status, "invalid", JSON.stringify(text));
    assert.ok(summary.intake_register_problem && !NO_CRASH.test(summary.intake_register_problem));
    assert.deepEqual(ids(records).sort(), annotationIds, "only the annotation records exist");
    assert.ok(warnings.some((w) => /intake register ignored/.test(w)));
    const gen = buildPack(m);
    assert.equal(gen.status, 1, `${JSON.stringify(text)}: ${gen.all}`);
    assert.match(gen.err, /REFUSED/);
    assert.doesNotMatch(gen.all, NO_CRASH);
    assert.ok(!fs.existsSync(m.packDir), "no pack output is written for an invalid register");
    const ap = applyExports(m.root, [exportFile("REV-001", { "SYNTH-AUTH-001": goodEntry() })]);
    assert.equal(ap.status, 1, `${JSON.stringify(text)}: ${ap.all}`);
    assert.match(ap.err, /intake register is invalid/);
    assert.doesNotMatch(ap.all, NO_CRASH);
    assert.ok(!fs.existsSync(m.verifiedFile) && !fs.existsSync(path.join(m.root, "corpus", "review_history.json")), "nothing written");
  }
});

/** One malformed entry next to a valid neighbour: every consumer copes, the bad entry is never loaded / offered / applied, the neighbour still works. */
async function checkRejectedEntry(label, entry, badId) {
  const m = fresh();
  writeRegister(m, regOf(validEntry("SYNTH-INTAKE-OK"), entry));
  const { records, summary, warnings } = await load(m);
  assert.equal(summary.intake_register_status, "ok", label);
  assert.equal(summary.skipped_intake_entries, 1, `${label}: exactly the malformed entry is skipped`);
  assert.ok(ids(records).includes("SYNTH-INTAKE-OK"), `${label}: the valid neighbour loads`);
  if (badId) assert.ok(!ids(records).includes(badId), `${label}: the malformed entry is not loaded`);
  assert.equal(records.filter((r) => String(r.split).startsWith("intake_")).length, 1, `${label}: only the valid entry became a record`);
  assert.equal(warnings.filter((w) => /skipped by the label loader/.test(w)).length, 1, label);
  const gen = buildPack(m, ["--scope", "all"]); // intake receipts are only offered outside the default 'authentic' scope
  assert.equal(gen.status, 0, `${label}: ${gen.all}`);
  assert.doesNotMatch(gen.all, NO_CRASH);
  if (badId) assert.ok(!offered(m).includes(badId), `${label}: not offered for review`);
  assert.ok(offered(m).includes("SYNTH-INTAKE-OK"), `${label}: the valid neighbour is offered`);
  assert.match(gen.out, /1 rejected by validation/);
  if (badId) {
    const bad = applyExports(m.root, [exportFile("REV-001", { [badId]: goodEntry() })]);
    assert.equal(bad.status, 1, `${label}: ${bad.all}`);
    assert.doesNotMatch(bad.all, NO_CRASH);
    assert.ok(!fs.existsSync(m.verifiedFile), `${label}: a rejected review writes nothing`);
    const mixed = applyExports(m.root, [exportFile("REV-001", { "SYNTH-INTAKE-OK": goodEntry(), [badId]: goodEntry() })]);
    assert.equal(mixed.status, 1, `${label}: one bad id rejects the whole invocation`);
    assert.ok(!fs.existsSync(m.verifiedFile), `${label}: no partial application`);
  }
  const ok = applyExports(m.root, [exportFile("REV-001", { "SYNTH-INTAKE-OK": goodEntry() })]);
  assert.equal(ok.status, 0, `${label}: ${ok.all}`);
  assert.deepEqual(Object.keys(readJson(m.verifiedFile).records), ["SYNTH-INTAKE-OK"]);
}

test("non-object / non-id entries: skipped, never crash anything", async () => {
  for (const [label, e] of [["null", null], ["string", "s"], ["number", 7], ["array", []], ["boolean", true]]) await checkRejectedEntry(label, e, null);
  for (const [label, e] of [["missing id", validEntry("x", { receipt_id: undefined })], ["null id", validEntry("x", { receipt_id: null })], ["blank id", validEntry("x", { receipt_id: "" })], ["whitespace id", validEntry("x", { receipt_id: "   " })], ["numeric id", validEntry("x", { receipt_id: 42 })]]) await checkRejectedEntry(label, e, null);
});

test("receipt_id with surrounding whitespace or an unreasonable length is rejected (identity would be ambiguous)", async () => {
  await checkRejectedEntry("surrounding whitespace", validEntry(" SYNTH-WS ", { image_path: "corpus/authentic_intake/pending/ws.png" }), " SYNTH-WS ");
  const long = "SYNTH-" + "X".repeat(120);
  await checkRejectedEntry("too long", validEntry(long, { image_path: "corpus/authentic_intake/pending/long.png" }), long);
});

test("image_path missing, blank, mistyped, absolute, traversing, backslashed, outside corpus/authentic_intake/, or malformed: entry rejected everywhere", async () => {
  const P = "corpus/authentic_intake/";
  const bad = [undefined, null, "", "   ", 5, {}, "C:\\x\\a.png", "/etc/a.png", "../a.png", `${P}../a.png`, "corpus\\authentic_intake\\pending\\a.png", "corpus/authentic/images/a.png", P, "corpus//authentic_intake/a.png", `./${P}a.png`, ` ${P}a.png`, `${P}a.png\n`, `${P}./a.png`, `${P}${"x".repeat(300)}`, "a.png"];
  for (const p of bad) await checkRejectedEntry(`image_path ${JSON.stringify(p)?.slice(0, 40)}`, validEntry("SYNTH-INTAKE-IMG", { image_path: p }), "SYNTH-INTAKE-IMG");
});

test("provenance missing, null, wrong type, empty, or not an authentic issued receipt: entry rejected everywhere", async () => {
  for (const [label, prov] of [["missing", undefined], ["null", null], ["string", "x"], ["array", []], ["empty object", {}], ["no type", { source_description: "d" }], ["synthetic type", { source_type: "synthetic_template" }], ["array type", { source_type: ["authentic_issued_receipt"] }], ["wrong case", { source_type: "AUTHENTIC_ISSUED_RECEIPT" }]]) {
    await checkRejectedEntry(`provenance ${label}`, validEntry("SYNTH-INTAKE-PROV", { provenance: prov }), "SYNTH-INTAKE-PROV");
  }
});

test("duplicate receipt ids in the register block ALL entries with that id (even identical valid ones); unique neighbours are unaffected", async () => {
  const m = fresh();
  writeRegister(m, regOf(validEntry("SYNTH-INTAKE-DUP"), validEntry("SYNTH-INTAKE-DUP", { receipt_family: "synthetic_family_b" }), validEntry("SYNTH-INTAKE-OK")));
  const { records, summary } = await load(m);
  assert.ok(!ids(records).includes("SYNTH-INTAKE-DUP"));
  assert.equal(summary.skipped_intake_entries, 2);
  assert.match(summary.skipped_intake_detail[0].reason, /more than once/);
  assert.ok(ids(records).includes("SYNTH-INTAKE-OK"));
  assert.equal(buildPack(m, ["--scope", "all"]).status, 0);
  assert.deepEqual(offered(m).filter((x) => x.startsWith("SYNTH-INTAKE")), ["SYNTH-INTAKE-OK"]);
  const r = applyExports(m.root, [exportFile("REV-001", { "SYNTH-INTAKE-DUP": goodEntry() })]);
  assert.equal(r.status, 1, r.all);
  assert.match(r.all, /more than once/);
  assert.ok(!fs.existsSync(m.verifiedFile));
});

test("an intake id that collides with an annotation id is ambiguous: the entry is skipped and that id is blocked everywhere (fail closed), the annotation record is untouched", async () => {
  for (const id of ["SYNTH-AUTH-001", "SYNTH-TPL-001", "SYNTH-FUEL-001", "SYNTH-CTRL-001"]) {
    const m = fresh();
    writeRegister(m, regOf(validEntry(id), validEntry("SYNTH-INTAKE-OK")));
    const { records, summary, L } = await load(m);
    assert.equal(records.filter((r) => r.receipt_id === id).length, 1, `${id}: no second record is created`);
    assert.ok(!String(records.find((r) => r.receipt_id === id).split).startsWith("intake_"));
    assert.equal(summary.skipped_intake_entries, 1);
    assert.match(summary.skipped_intake_detail[0].reason, /collides/);
    assert.equal(buildPack(m).status, 0);
    assert.ok(!offered(m).includes(id), `${id}: not offered while its id is ambiguous`);
    const r = applyExports(m.root, [exportFile("REV-001", { [id]: goodEntry() })]);
    assert.equal(r.status, 1, `${id}: ${r.all}`);
    assert.doesNotMatch(r.all, NO_CRASH);
    assert.ok(!fs.existsSync(m.verifiedFile));
    assert.ok(L.verificationEligibility(readJson(path.join(m.root, "corpus", "annotations.json")).records, readJson(path.join(m.root, "intake", "intake_register.json")).receipts).blocked.has(id));
  }
});

test("pack and applier agree on a mixed register: the pack offers exactly what the applier accepts; everything else is refused with nothing written", async () => {
  const m = fresh();
  const register = regOf(
    validEntry("SYNTH-INTAKE-A"), validEntry("SYNTH-INTAKE-B", { split: "heldout_untouched", receipt_family: "synthetic_family_b" }),
    validEntry("SYNTH-INTAKE-NOPROV", { provenance: undefined }), validEntry("SYNTH-INTAKE-BADIMG", { image_path: "../escape.png" }),
    validEntry("SYNTH-INTAKE-DUP"), validEntry("SYNTH-INTAKE-DUP"), validEntry("SYNTH-AUTH-002"), null, validEntry("SYNTH-INTAKE-NOFILE"),
  );
  writeRegister(m, register, ["SYNTH-INTAKE-NOFILE"]);
  const { summary } = await load(m);
  assert.equal(summary.skipped_intake_entries, 6, "no-provenance, bad image, 2x dup, collision, null");
  for (const scope of ["all", "pending", "authentic"]) {
    const g = buildPack(m, ["--scope", scope]);
    assert.equal(g.status, 0, `${scope}: ${g.all}`);
    const o = offered(m);
    assert.equal(new Set(o).size, o.length, `${scope}: no id is offered twice`);
    const expected = { all: ["SYNTH-AUTH-001", "SYNTH-INTAKE-A", "SYNTH-INTAKE-B", "SYNTH-TPL-001"], pending: ["SYNTH-INTAKE-A", "SYNTH-INTAKE-B"], authentic: ["SYNTH-AUTH-001", "SYNTH-TPL-001"] }[scope];
    assert.deepEqual(o, expected, scope);
    if (scope !== "authentic") assert.match(g.err, /SYNTH-INTAKE-NOFILE: not offered \(image file not found\)/);
  }
  buildPack(m, ["--scope", "all"]);
  const manifest = manifestOf(m);
  for (const it of manifest.items) {
    const abs = path.resolve(m.packDir, it.image);
    assert.ok(abs.startsWith(path.resolve(m.root) + path.sep) && fs.existsSync(abs), `${it.id}: image resolves to an existing file inside the mirror`);
    assert.equal(it.kind, it.id.startsWith("SYNTH-INTAKE") ? "intake" : "existing");
  }
  const everyOffered = Object.fromEntries(manifest.items.map((it) => [it.id, goodEntry()]));
  const refused = ["SYNTH-INTAKE-NOPROV", "SYNTH-INTAKE-BADIMG", "SYNTH-INTAKE-DUP", "SYNTH-AUTH-002", "SYNTH-FUEL-001", "SYNTH-CTRL-001", "SYNTH-INTAKE-NOFILE"]; // NOFILE: structurally valid but its image is unavailable
  for (const id of refused) {
    const r = applyExports(m.root, [exportFile("REV-001", { [id]: goodEntry() })]);
    assert.equal(r.status, 1, `${id} must be refused: ${r.all}`);
    assert.doesNotMatch(r.all, NO_CRASH);
  }
  const partial = applyExports(m.root, [exportFile("REV-001", { ...everyOffered, "SYNTH-INTAKE-NOPROV": goodEntry() })]);
  assert.equal(partial.status, 1);
  assert.ok(!fs.existsSync(m.verifiedFile) && !fs.existsSync(path.join(m.root, "corpus", "review_history.json")), "no partial label application when any named receipt is refused");
  const all = applyExports(m.root, [exportFile("REV-001", everyOffered)]);
  assert.equal(all.status, 0, `every offered receipt is accepted: ${all.all}`);
  assert.deepEqual(Object.keys(readJson(m.verifiedFile).records).sort(), Object.keys(everyOffered).sort());
  buildPack(m, ["--scope", "all"]);
  assert.deepEqual(offered(m), [], "receipts that are already human-verified are not offered again");
});

test("valid intake entries keep working exactly as before (loader record, pack item with its own image path, applier)", async () => {
  const m = fresh();
  writeRegister(m, regOf(validEntry("SYNTH-INTAKE-A"), validEntry("SYNTH-INTAKE-B", { split: "heldout_untouched", receipt_family: "synthetic_family_b", brand_family: "Nayara" })));
  const { records, summary, warnings } = await load(m);
  assert.equal(summary.intake_register_status, "ok");
  assert.equal(summary.intake_register_problem, null);
  assert.equal(summary.skipped_intake_entries, 0);
  assert.deepEqual(warnings, []);
  const a = records.find((r) => r.receipt_id === "SYNTH-INTAKE-A"), b = records.find((r) => r.receipt_id === "SYNTH-INTAKE-B");
  assert.equal(a.split, "intake_dev");
  assert.equal(b.split, "intake_heldout");
  assert.equal(a.annotation_status, "pending_human_label");
  assert.ok(Object.values(a.fields).every((f) => f.excluded && f.normalized_value === null));
  assert.equal(buildPack(m, ["--scope", "pending"]).status, 0);
  const mf = manifestOf(m);
  assert.deepEqual(mf.items.map((x) => x.id).sort(), ["SYNTH-INTAKE-A", "SYNTH-INTAKE-B"]);
  assert.ok(mf.items.every((x) => x.kind === "intake" && x.image.endsWith(`corpus/authentic_intake/pending/${x.id}.png`) && x.provisional === null));
  assert.equal(applyExports(m.root, [exportFile("REV-001", { "SYNTH-INTAKE-A": goodEntry(), "SYNTH-INTAKE-B": goodEntry() })]).status, 0);
  assert.equal((await load(m)).summary.human_verified, 2);
});

test("a register written by the REAL writer (scripts/register_intake.mjs, pointed at the mirror with --fuel-dir) passes the new validation", async () => {
  const m = fresh();
  const PNG = Buffer.from("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg==", "base64");
  const prov = { source_description: "synthetic fixture", source_type: "authentic_issued_receipt", owner: "TEST OWNER", permission_basis: "test", authorized_by: "TEST AUTHORIZER", retrieval_date: "2026-10-22", brand_family: "Nayara", receipt_family: "synthetic_nayara_a", personal_data_handling: "none", permitted_usage: { store: true, modify: false, evaluate: true, redistribute: false } };
  for (const [i, split] of ["development", "heldout_untouched"].entries()) {
    const img = path.join(m.root, "in", `real-writer-${i}.png`), pj = path.join(m.root, "in", `real-writer-${i}.json`);
    fs.writeFileSync(img, Buffer.concat([PNG, Buffer.from(`distinct-${i}`)]));
    fs.writeFileSync(pj, JSON.stringify({ ...prov, receipt_family: `synthetic_nayara_${i}` }));
    for (const p of [img, pj]) assert.ok(path.resolve(p).startsWith(path.resolve(m.root) + path.sep));
    const r = spawnSync("node", [path.join(REAL_FUEL, "scripts", "register_intake.mjs"), img, "--provenance", pj, "--split", split, "--fuel-dir", m.root], { encoding: "utf8" });
    assert.equal(r.status, 0, r.stdout + r.stderr);
  }
  const { records, summary } = await load(m);
  assert.equal(summary.skipped_intake_entries, 0);
  assert.equal(records.filter((r) => String(r.split).startsWith("intake_")).length, 2);
  assert.equal(buildPack(m, ["--scope", "pending"]).status, 0);
  assert.equal(offered(m).length, 2);
});

// ============ image AVAILABILITY (a filesystem property, checked at the I/O boundaries) vs structural validity (pure) ============
const PNG_BYTES = Buffer.from("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg==", "base64");
const imgAbs = (m, id) => path.join(m.root, "corpus", "authentic_intake", "pending", `${id}.png`);
const REASON = { notFound: "image file not found", notFile: "image unavailable (not a regular file)", empty: "image unavailable (empty file)" };
/** sha256 of every file under the mirror except in/ (where applyExports drops its input): proves that a rejected invocation wrote NOTHING. */
function treeState(root) {
  const out = {};
  const walk = (d) => {
    for (const e of fs.readdirSync(d, { withFileTypes: true })) {
      const p = path.join(d, e.name), rel = path.relative(root, p);
      if (rel.split(path.sep)[0] === "in") continue;
      if (e.isSymbolicLink()) out[rel] = "link";
      else if (e.isDirectory()) { out[`${rel}/`] = "dir"; walk(p); } else out[rel] = crypto.createHash("sha256").update(fs.readFileSync(p)).digest("hex");
    }
  };
  walk(root);
  return out;
}
const writtenNothing = (m, before, label) => {
  assert.deepEqual(treeState(m.root), before, `${label}: the mirror must be byte-identical outside in/ - nothing was written`);
  for (const f of ["verified_labels.json", "review_history.json", "review_conflicts.json", "adjudication_needed.json"]) assert.ok(!fs.existsSync(path.join(m.root, "corpus", f)) || before[path.join("corpus", f)], `${label}: ${f} must not have been created`);
};

test("image availability (a): structural validity is independent of the filesystem - a valid entry whose image is missing still loads as a PENDING record", async () => {
  const m = fresh();
  writeRegister(m, regOf(validEntry("SYNTH-INTAKE-NOFILE")), ["SYNTH-INTAKE-NOFILE"]);
  assert.ok(!fs.existsSync(imgAbs(m, "SYNTH-INTAKE-NOFILE")));
  const { records, summary, warnings, L } = await load(m);
  assert.equal(summary.intake_register_status, "ok");
  assert.equal(summary.skipped_intake_entries, 0);
  assert.deepEqual(warnings, [], "the loader says nothing about image files");
  const r = records.find((x) => x.receipt_id === "SYNTH-INTAKE-NOFILE");
  assert.equal(r.annotation_status, "pending_human_label");
  assert.equal(r.split, "intake_dev");
  assert.ok(Object.values(r.fields).every((f) => f.excluded === true && f.normalized_value === null));
  const reg = readJson(path.join(m.root, "intake", "intake_register.json")).receipts;
  assert.equal(IR.inspectIntakeEntries(reg).usable.length, 1, "the pure structural inspector takes no directory and sees a usable entry");
  assert.ok(L.verificationEligibility([], reg).eligible.has("SYNTH-INTAKE-NOFILE"), "structural eligibility does not depend on the image");
});

test("image availability (b)+(c): the applier rejects a NAMED missing-image entry with nothing written; the same entry is accepted once a non-empty placeholder image exists", () => {
  const m = fresh();
  const reg = regOf(validEntry("SYNTH-INTAKE-NOFILE"));
  writeRegister(m, reg, ["SYNTH-INTAKE-NOFILE"]);
  const before = treeState(m.root);
  const r = applyExports(m.root, [exportFile("REV-001", { "SYNTH-INTAKE-NOFILE": goodEntry() })]);
  assert.equal(r.status, 1, r.all);
  assert.match(r.err, /REJECTED - nothing was written/);
  assert.ok(r.err.includes(`receipt SYNTH-INTAKE-NOFILE: ${REASON.notFound}`), r.err);
  assert.doesNotMatch(r.all, NO_CRASH);
  writtenNothing(m, before, "missing image");
  placeIntakeImages(m.root, reg); // now a blank (non-empty) placeholder exists
  const ok = applyExports(m.root, [exportFile("REV-001", { "SYNTH-INTAKE-NOFILE": goodEntry() })]);
  assert.equal(ok.status, 0, ok.all);
  assert.deepEqual(Object.keys(readJson(m.verifiedFile).records), ["SYNTH-INTAKE-NOFILE"]);
});

test("image availability (d): one available + one unavailable image in the same export rejects the WHOLE invocation and writes nothing (also alongside an ineligible id)", () => {
  const m = fresh();
  writeRegister(m, regOf(validEntry("SYNTH-INTAKE-OK"), validEntry("SYNTH-INTAKE-NOFILE")), ["SYNTH-INTAKE-NOFILE"]);
  const before = treeState(m.root);
  const r = applyExports(m.root, [exportFile("REV-001", { "SYNTH-INTAKE-OK": goodEntry(), "SYNTH-INTAKE-NOFILE": goodEntry() })]);
  assert.equal(r.status, 1, r.all);
  assert.ok(r.err.includes(`receipt SYNTH-INTAKE-NOFILE: ${REASON.notFound}`));
  assert.ok(!r.err.includes("receipt SYNTH-INTAKE-OK"), "the available entry is not blamed");
  writtenNothing(m, before, "mixed export");
  const both = applyExports(m.root, [exportFile("REV-001", { "SYNTH-INTAKE-NOFILE": goodEntry(), "SYNTH-FUEL-001": goodEntry() })]);
  assert.equal(both.status, 1);
  assert.ok(both.err.includes(REASON.notFound) && both.err.includes("SYNTH-FUEL-001") && /not eligible/.test(both.err), "ineligible and unavailable are reported together in one rejection");
  writtenNothing(m, before, "ineligible + unavailable");
});

test("image availability (e): a missing image for an id the export does NOT name does not block unrelated valid receipts", () => {
  const m = fresh();
  writeRegister(m, regOf(validEntry("SYNTH-INTAKE-OK"), validEntry("SYNTH-INTAKE-NOFILE")), ["SYNTH-INTAKE-NOFILE"]);
  const r = applyExports(m.root, [exportFile("REV-001", { "SYNTH-INTAKE-OK": goodEntry(), "SYNTH-AUTH-001": goodEntry() })]);
  assert.equal(r.status, 0, r.all);
  assert.deepEqual(Object.keys(readJson(m.verifiedFile).records).sort(), ["SYNTH-AUTH-001", "SYNTH-INTAKE-OK"]);
});

test("image availability: an ALREADY-VERIFIED entry whose image later disappears is rejected when named again (even an identical re-application), with nothing written", () => {
  const m = fresh();
  writeRegister(m, regOf(validEntry("SYNTH-INTAKE-OK")));
  assert.equal(applyExports(m.root, [exportFile("REV-001", { "SYNTH-INTAKE-OK": goodEntry() })]).status, 0);
  fs.unlinkSync(imgAbs(m, "SYNTH-INTAKE-OK")); // removed after registration AND after verification
  const before = treeState(m.root);
  const r = applyExports(m.root, [exportFile("REV-001", { "SYNTH-INTAKE-OK": goodEntry() })]);
  assert.equal(r.status, 1, r.all);
  assert.ok(r.err.includes(`receipt SYNTH-INTAKE-OK: ${REASON.notFound}`));
  assert.deepEqual(treeState(m.root), before, "nothing written (no history event, no rewrite)");
});

test("image availability (f): intakeImageProblem - missing file, directory, zero-byte file, file removed after registration, valid non-empty file, and hostile inputs", () => {
  const m = fresh();
  const dir = path.join(m.root, "corpus", "authentic_intake", "pending");
  fs.mkdirSync(dir, { recursive: true });
  const e = (name) => ({ image_path: `corpus/authentic_intake/pending/${name}` });
  assert.equal(IR.intakeImageProblem(m.root, e("absent.png")), REASON.notFound);
  fs.mkdirSync(path.join(dir, "a-directory.png"));
  assert.equal(IR.intakeImageProblem(m.root, e("a-directory.png")), REASON.notFile);
  fs.writeFileSync(path.join(dir, "zero.png"), "");
  assert.equal(IR.intakeImageProblem(m.root, e("zero.png")), REASON.empty);
  fs.writeFileSync(path.join(dir, "ok.png"), PNG_BYTES);
  assert.equal(IR.intakeImageProblem(m.root, e("ok.png")), null);
  fs.writeFileSync(path.join(dir, "gone.png"), PNG_BYTES);
  assert.equal(IR.intakeImageProblem(m.root, e("gone.png")), null);
  fs.unlinkSync(path.join(dir, "gone.png"));
  assert.equal(IR.intakeImageProblem(m.root, e("gone.png")), REASON.notFound, "removed after registration");
  // paths that are not valid intake image paths are unavailable even if a file exists there, and nothing throws on hostile input
  fs.writeFileSync(path.join(m.root, "outside.png"), PNG_BYTES);
  for (const bad of ["corpus/authentic_intake/../../outside.png", "../outside.png", "outside.png", "C:\\outside.png", "/outside.png", "corpus/authentic_intake/", "", null, undefined, 5]) {
    const p = IR.intakeImageProblem(m.root, { image_path: bad });
    assert.ok(typeof p === "string" && p.length > 0, `image_path ${JSON.stringify(bad)} must be unavailable`);
  }
  for (const entry of [null, undefined, 7, "str", [], {}]) assert.equal(typeof IR.intakeImageProblem(m.root, entry), "string");
  assert.equal(typeof IR.intakeImageProblem(path.join(m.root, "no-such-dir"), e("ok.png")), "string");
});

test("image availability (f2): a directory junction / symlink that resolves OUTSIDE corpus/authentic_intake/ is unavailable; one that stays inside is fine", () => {
  const m = fresh();
  const root = path.join(m.root, "corpus", "authentic_intake"), pending = path.join(root, "pending");
  fs.mkdirSync(pending, { recursive: true });
  const outside = path.join(m.root, "outside_dir");
  fs.mkdirSync(outside); fs.writeFileSync(path.join(outside, "a.png"), PNG_BYTES);
  fs.symlinkSync(outside, path.join(pending, "escape"), "junction"); // directory junctions need no privilege on Windows; a plain dir symlink elsewhere
  const escaped = IR.intakeImageProblem(m.root, { image_path: "corpus/authentic_intake/pending/escape/a.png" });
  assert.equal(escaped, "image unavailable (resolves outside corpus/authentic_intake/)");
  const inside = path.join(pending, "real_dir");
  fs.mkdirSync(inside); fs.writeFileSync(path.join(inside, "a.png"), PNG_BYTES);
  fs.symlinkSync(inside, path.join(pending, "alias"), "junction");
  assert.equal(IR.intakeImageProblem(m.root, { image_path: "corpus/authentic_intake/pending/alias/a.png" }), null, "an alias that stays inside the intake folder is available");
  // and the applier refuses an entry that escapes through the junction (nothing written)
  const reg = regOf(validEntry("SYNTH-INTAKE-ESC", { image_path: "corpus/authentic_intake/pending/escape/a.png" }));
  writeRegister(m, reg);
  const before = treeState(m.root);
  const r = applyExports(m.root, [exportFile("REV-001", { "SYNTH-INTAKE-ESC": goodEntry() })]);
  assert.equal(r.status, 1, r.all);
  assert.ok(r.err.includes("resolves outside corpus/authentic_intake/"));
  assert.deepEqual(treeState(m.root), before);
});

test("image availability (f3): a FILE symlink escaping the folder, and a broken file symlink, are unavailable (skipped where the OS needs privilege to create symlinks)", (t) => {
  const m = fresh();
  const pending = path.join(m.root, "corpus", "authentic_intake", "pending");
  fs.mkdirSync(pending, { recursive: true });
  const outsideFile = path.join(m.root, "outside.png");
  fs.writeFileSync(outsideFile, PNG_BYTES);
  try {
    fs.symlinkSync(outsideFile, path.join(pending, "escape.png"), "file");
    fs.symlinkSync(path.join(m.root, "does-not-exist.png"), path.join(pending, "broken.png"), "file");
  } catch { t.skip("file symlinks cannot be created here (privilege)"); return; }
  assert.equal(IR.intakeImageProblem(m.root, { image_path: "corpus/authentic_intake/pending/escape.png" }), "image unavailable (resolves outside corpus/authentic_intake/)");
  assert.equal(IR.intakeImageProblem(m.root, { image_path: "corpus/authentic_intake/pending/broken.png" }), "image unavailable (broken symlink)");
});

test("image availability (f4): a link NAMED like the image (a directory junction needs no privilege) that dangles or points outside is unavailable - exercises the broken-link and escape branches", (t) => {
  const m = fresh();
  const pending = path.join(m.root, "corpus", "authentic_intake", "pending");
  fs.mkdirSync(pending, { recursive: true });
  const outsideDir = path.join(m.root, "outside_dir");
  fs.mkdirSync(outsideDir);
  try {
    fs.symlinkSync(path.join(m.root, "dangling_target"), path.join(pending, "dangling.png"), "junction"); // target does not exist
    fs.symlinkSync(outsideDir, path.join(pending, "points-outside.png"), "junction");
  } catch { t.skip("junctions cannot be created here"); return; }
  assert.equal(IR.intakeImageProblem(m.root, { image_path: "corpus/authentic_intake/pending/dangling.png" }), "image unavailable (broken symlink)");
  assert.equal(IR.intakeImageProblem(m.root, { image_path: "corpus/authentic_intake/pending/points-outside.png" }), "image unavailable (resolves outside corpus/authentic_intake/)");
});

test("image availability (g): pack and applier agree - every intake entry the pack does NOT offer is refused by the applier with nothing written; every one it offers is accepted (scopes pending, all, authentic)", async () => {
  const m = fresh();
  const A = "SYNTH-INTAKE-A", B = "SYNTH-INTAKE-B", NOFILE = "SYNTH-INTAKE-NOFILE", DIR = "SYNTH-INTAKE-DIR", EMPTY = "SYNTH-INTAKE-EMPTY", BADPROV = "SYNTH-INTAKE-BADPROV";
  const reg = regOf(validEntry(A), validEntry(B, { split: "heldout_untouched", receipt_family: "synthetic_family_b" }), validEntry(NOFILE), validEntry(DIR), validEntry(EMPTY), validEntry(BADPROV, { provenance: null }));
  writeRegister(m, reg, [NOFILE, DIR, EMPTY]);
  fs.mkdirSync(imgAbs(m, DIR));          // a directory where the image should be
  fs.writeFileSync(imgAbs(m, EMPTY), ""); // a zero-byte file
  const intakeIds = reg.receipts.map((x) => x.receipt_id);
  const reasons = { [NOFILE]: REASON.notFound, [DIR]: REASON.notFile, [EMPTY]: REASON.empty };
  const offeredIntake = {};
  for (const scope of ["pending", "all", "authentic"]) {
    const g = buildPack(m, ["--scope", scope]);
    assert.equal(g.status, 0, `${scope}: ${g.all}`);
    offeredIntake[scope] = offered(m).filter((id) => intakeIds.includes(id));
    if (scope !== "authentic") for (const [id, reason] of Object.entries(reasons)) assert.ok(g.err.includes(`intake receipt ${id}: not offered (${reason})`), `${scope}: warning for ${id}`);
  }
  assert.deepEqual(offeredIntake.pending, [A, B]);
  assert.deepEqual(offeredIntake.all, [A, B]);
  assert.deepEqual(offeredIntake.authentic, [], "intake receipts are not part of the default 'authentic' scope, by design");
  const before = treeState(m.root);
  for (const id of intakeIds.filter((x) => !offeredIntake.all.includes(x))) {
    const r = applyExports(m.root, [exportFile("REV-001", { [id]: goodEntry() })]);
    assert.equal(r.status, 1, `${id} (not offered) must be refused: ${r.all}`);
    assert.ok(r.err.includes(`receipt ${id}:`) && (reasons[id] ? r.err.includes(reasons[id]) : true), `${id}: ${r.err}`);
  }
  assert.deepEqual(treeState(m.root), before, "all refusals together wrote nothing");
  for (const id of offeredIntake.all) assert.equal(applyExports(m.root, [exportFile("REV-001", { [id]: goodEntry() })]).status, 0, `${id} (offered) must be accepted`);
  assert.deepEqual(Object.keys(readJson(m.verifiedFile).records).sort(), [A, B]);
});
