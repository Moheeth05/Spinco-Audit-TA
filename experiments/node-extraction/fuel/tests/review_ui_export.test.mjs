// Review UI export validation. Pack generation runs from a synthetic MIRROR tree (tests/helpers/syntheticMirror.mjs), never against the real corpus.
// DOM behaviour (a status change clearing its value) cannot run in plain Node, so it is covered by the browser test at the bottom (Playwright via
// backend/.venv; skipped, and reported as skipped, if unavailable) - see tests/review_tool_smoke.py.
import test from "node:test";
import assert from "node:assert/strict";
import crypto from "node:crypto";
import fs from "node:fs";
import path from "node:path";
import vm from "node:vm";
import { spawnSync } from "node:child_process";
import { makeMirror, assertIsolated, runScript, applyExports, goodEntry, readJson, REAL_FUEL } from "./helpers/syntheticMirror.mjs";
import * as RV from "../src/reviewValidation.mjs";

const m = makeMirror();
assertIsolated(m.root);
const gen = runScript(m.root, "build_review_pack.mjs", ["--out", m.packDir]);
const html = fs.existsSync(path.join(m.packDir, "review.html")) ? fs.readFileSync(path.join(m.packDir, "review.html"), "utf8") : "";
const manifest = fs.existsSync(path.join(m.packDir, "manifest.js")) ? JSON.parse(fs.readFileSync(path.join(m.packDir, "manifest.js"), "utf8").replace(/^window\.REVIEW_MANIFEST = /, "").replace(/;\s*$/, "")) : null;

const items = [{ id: "SYNTH-AUTH-001" }, { id: "SYNTH-AUTH-002" }, { id: "SYNTH-TPL-001" }];
const build = (state, o = {}) => RV.buildExportPayload({ items, state, reviewerId: "REV-001", authorizationConfirmed: true, packId: "synthetic-pack", blind: true, exportedAt: "2026-10-22T10:00:00Z", ...o });
const withField = (field, patch) => ({ ...goodEntry(), [field]: { ...goodEntry()[field], ...patch } });

test("synthetic pack generated from the mirror: only verifiable receipts, images resolve inside the mirror, validator embedded", () => {
  assert.equal(gen.status, 0, gen.all);
  assert.deepEqual(manifest.items.map((x) => x.id).sort(), ["SYNTH-AUTH-001", "SYNTH-AUTH-002", "SYNTH-TPL-001"], "synthetic and control records are not offered for review");
  for (const it of manifest.items) assert.ok(fs.existsSync(path.resolve(m.packDir, it.image)) && path.resolve(m.packDir, it.image).startsWith(path.resolve(m.root)), `image for ${it.id} must exist inside the mirror`);
  assert.equal(manifest.validator_sha256, crypto.createHash("sha256").update(fs.readFileSync(path.join(REAL_FUEL, "src", "reviewValidation.mjs"))).digest("hex"));
  assert.ok(!html.includes("__RV_INLINE__"), "the validator placeholder must be replaced");
  assert.ok(html.includes('id="errors"') && html.includes("ReviewValidation.buildExportPayload") && html.includes("clearValues"));
  assert.ok(!/(src|href)\s*=\s*["']https?:/i.test(html) && !/@import|fetch\(|XMLHttpRequest|WebSocket/.test(html), "no network references");
});

test("the validator embedded in the page behaves exactly like src/reviewValidation.mjs", () => {
  const inline = html.match(/<script id="rv">([\s\S]*?)<\/script>/)[1];
  const E = vm.runInNewContext(inline + "\nReviewValidation", {});
  const rt = (x) => JSON.parse(JSON.stringify(x));
  for (const d of ["2026-09-24", "2024-02-29", "2025-02-29", "2026-13-45", "24/09/2026", "0026-09-24", "", null]) assert.equal(E.isRealIsoDate(d), RV.isRealIsoDate(d), String(d));
  for (const id of ["REV-001", "x", "  a b c ", "bad/id", "a+b", 5]) assert.deepEqual(rt(E.validateReviewerId(id)), rt(RV.validateReviewerId(id)), String(id));
  const cases = [goodEntry(), withField("pump_name", { status: "banana" }), withField("total_amount", { value: null }), withField("transaction_date", { iso_date: "2026-02-30" }), withField("total_amount", { status: "illegible" }), withField("pump_name", { status: "not_printed", value: null }), {}];
  for (const c of cases) assert.deepEqual(rt(E.validateReviewFields(c)), rt(RV.validateReviewFields(c)));
  const args = { items, state: { "SYNTH-AUTH-001": goodEntry(), "SYNTH-AUTH-002": withField("pump_name", { status: "banana" }) }, reviewerId: "REV-001", authorizationConfirmed: true, packId: "p", blind: true, exportedAt: "t" };
  assert.deepEqual(rt(E.buildExportPayload(args)), rt(RV.buildExportPayload(args)));
});

test("valid synthetic review: export is allowed, keeps the existing schema and values, and is accepted by apply_verification in the mirror", () => {
  const state = { "SYNTH-AUTH-001": goodEntry(), "SYNTH-AUTH-002": { ...goodEntry(), pump_name: { status: "read", value: "SYNTHETIC DEALER TWO" }, transaction_date: { status: "read", iso_date: "2026-08-25", order_provable_from_receipt: true, order_evidence: "component_gt_12" }, total_amount: { status: "read", value: 200 } } };
  const res = build(state, { reviewerId: "  REV-001 " });
  assert.equal(res.ok, true, JSON.stringify(res.errors));
  assert.deepEqual(res.incomplete, ["SYNTH-TPL-001"], "a receipt with no statuses is left out, as before");
  assert.equal(res.payload.schema, "fuel-review-export/v1");
  assert.equal(res.payload.reviewer_id, "REV-001");
  assert.equal(res.payload.authorization_confirmed, true);
  assert.deepEqual(Object.keys(res.payload.records), ["SYNTH-AUTH-001", "SYNTH-AUTH-002"]);
  assert.deepEqual(res.payload.records["SYNTH-AUTH-001"], goodEntry(), "valid entries are exported unchanged");
  const r = applyExports(m.root, [res.payload]);
  assert.equal(r.status, 0, r.all);
});

test("invalid synthetic reviews are blocked (nothing to download) with messages that name the rule, not the values", () => {
  const SECRET = "SYNTHETIC-SECRET-VALUE-9";
  const cases = [
    ["status read but value cleared", withField("pump_name", { value: null }), /pump_name/],
    ["unsupported status", withField("pump_name", { status: "banana", value: SECRET }), /pump_name: unsupported status/],
    ["impossible date", withField("transaction_date", { iso_date: "2026-02-30" }), /real calendar date/],
    ["empty date with status read", withField("transaction_date", { iso_date: null }), /real calendar date/],
    ["non-numeric amount", withField("total_amount", { value: "abc" }), /total_amount: .*numeric/],
    ["negative amount", withField("total_amount", { value: -1 }), /greater than 0/],
    ["amount with 3 decimals", withField("total_amount", { value: 10.123 }), /two decimal/],
    ["stale pump value left on an illegible field", withField("pump_name", { status: "illegible", value: SECRET }), /contradicts/],
    ["stale pump value left on a not-printed field", withField("pump_name", { status: "not_printed", value: SECRET }), /contradicts/],
    ["stale amount left on an illegible field", withField("total_amount", { status: "illegible" }), /contradicts/],
    ["stale date left on an illegible field", withField("transaction_date", { status: "illegible" }), /contradicts/],
    ["provable order with contradicting evidence", withField("transaction_date", { order_provable_from_receipt: true, order_evidence: "none" }), /provable/],
  ];
  for (const [name, entry, re] of cases) {
    const res = build({ "SYNTH-AUTH-001": entry });
    assert.equal(res.ok, false, name);
    assert.match(res.errors.join("\n"), re, name);
    assert.match(res.errors.join("\n"), /SYNTH-AUTH-001/, name);
    assert.ok(!res.errors.join("\n").includes(SECRET), `${name}: the message must not echo values`);
    assert.equal(res.payload, undefined, `${name}: no payload may exist for an invalid review`);
  }
});

test("an invalid receipt blocks the whole export even when other receipts are valid", () => {
  const res = build({ "SYNTH-AUTH-001": goodEntry(), "SYNTH-AUTH-002": withField("total_amount", { value: null }) });
  assert.equal(res.ok, false);
  assert.equal(res.payload, undefined);
});

test("missing statuses: a receipt without all three statuses is incomplete (excluded); nothing complete means nothing to export", () => {
  const partial = { pump_name: { status: "read", value: "SYNTHETIC DEALER ONE" }, transaction_date: { status: null }, total_amount: { status: "read", value: 100 } };
  const a = build({ "SYNTH-AUTH-001": goodEntry(), "SYNTH-AUTH-002": partial });
  assert.equal(a.ok, true);
  assert.deepEqual(Object.keys(a.payload.records), ["SYNTH-AUTH-001"]);
  assert.ok(a.incomplete.includes("SYNTH-AUTH-002"));
  const b = build({ "SYNTH-AUTH-002": partial });
  assert.equal(b.ok, false);
  assert.match(b.errors.join("\n"), /no complete receipt/);
  assert.equal(build({}).ok, false);
  assert.equal(build(undefined).ok, false);
});

test("reviewer id and authorization are validated before anything can be exported", () => {
  const state = { "SYNTH-AUTH-001": goodEntry() };
  for (const id of ["", "  ", "x", "bad/id", "a+b"]) assert.match(build(state, { reviewerId: id }).errors.join("\n"), /reviewer_id/, id);
  assert.match(build(state, { authorizationConfirmed: false }).errors.join("\n"), /authorization box/);
  assert.equal(build(state).ok, true);
});

test("a pack built from the mirror never touches the real review pack directory (only the mirror was written)", () => {
  assert.ok(path.resolve(m.packDir).startsWith(path.resolve(m.root)));
  // outside means: a ".."-relative path, or (on another drive, e.g. the OS temp dir) an absolute one - path.relative returns the latter across drives
  const rel = path.relative(path.join(REAL_FUEL, "verification"), m.root);
  assert.ok(rel.startsWith("..") || path.isAbsolute(rel), "the mirror is not inside the real verification/ directory");
});

// ---- browser (DOM) behaviour -----------------------------------------------------------------------------------------------------
const py = path.resolve(REAL_FUEL, "..", "..", "..", "backend", ".venv", "Scripts", "python.exe");
const smoke = path.join(REAL_FUEL, "tests", "review_tool_smoke.py");
test("browser: status changes clear values, invalid exports are blocked, a valid export downloads, no network request", { skip: !fs.existsSync(py) && "backend/.venv python (Playwright) not found" }, () => {
  const r = spawnSync(py, [smoke, path.join(m.packDir, "review.html")], { encoding: "utf8", timeout: 240000 });
  const out = (r.stdout || "") + (r.stderr || "");
  assert.doesNotMatch(out, /^SKIP:/m, "no browser available - the DOM checks did not run: " + out.slice(0, 200));
  assert.doesNotMatch(out, /^FAIL /m, out);
  assert.equal(r.status, 0, out);
  assert.match(out, /PASS .*read -> illegible clears the pump value/);
});
