// Preflight for scripts/run_eval.mjs: every check that can be done BEFORE any OCR runs and before any report file is written. run_eval refuses (exit 1, nothing
// started, nothing written) if this returns a single problem. Messages name the receipt / rule and never contain image content.
//
// What it validates (the shared structural / availability / integrity code lives in src/intakeRegister.mjs and is reused, not copied):
//   * the command-line values (an unknown --only / --impl / --ocr / --labels used to be silently treated as a default; --label names files under reports/);
//   * the intake register as a whole (invalid register, or ANY skipped entry: conflicting duplicates, cross-split families, malformed entries) - an evaluation that
//     silently dropped intake receipts would be misleading;
//   * a non-empty selection, and a readable corpus/ocr-index.json (it is read after the OCR loop, so a bad one used to fail only after all the OCR work);
//   * every image the SELECTED evaluation will OCR: intake receipts via intakeImageIntegrityProblem (available AND matching the register's SHA-256); corpus images
//     (authentic / synthetic / control) must exist as regular, non-empty files inside corpus/ (they carry no per-image hash in the annotations, so no hash is checked);
//   * the held-out lock: if any held-out INTAKE receipt is selected a valid lock is REQUIRED, and the register's held-out entries must equal the lock's receipts (same ids,
//     and the same sha256 / family / brand), so an image or register entry changed after locking is caught against the LOCK, not just against the register. A lock file
//     that exists but is malformed is refused whatever is selected (run_eval reads it unconditionally).
// What the lock can and cannot prove: it records, per held-out receipt, the sha256 / family / brand copied from the register when it was created (verified against the
// image bytes at that moment by lock_heldout.mjs), plus code hashes. It is plain unanchored JSON: it proves what was RECORDED, not that nobody edited both it and the images.
import fs from "node:fs";
import path from "node:path";
import { intakeImageIntegrityProblem } from "./intakeRegister.mjs";

export const ONLY_VALUES = ["all", "dev", "heldout", "heldout2", "heldout3", "heldout4"];
export const IMPL_VALUES = ["baseline", "improved"];
export const OCR_MODES = ["single", "retry", "retry2"]; // the modes src/ocr.mjs implements
export const LABEL_MODES = ["all", "verified_only"];
export const LABEL_RE = /^[A-Za-z0-9][A-Za-z0-9._-]{0,80}$/;
export const HELDOUT_INTAKE_GROUP = "authentic_intake_heldout";
const HEX64 = /^[0-9a-f]{64}$/;
const isObj = (v) => v !== null && typeof v === "object" && !Array.isArray(v);
const isInside = (dir, p) => { const rel = path.relative(dir, p); return rel === "" || (!rel.startsWith("..") && !path.isAbsolute(rel)); };

/** Command-line problems (pure). `labels` is --labels (undefined = from the policy file). */
export function argProblems({ impl, only, label, ocrMode, labels }) {
  const out = [];
  if (!IMPL_VALUES.includes(impl)) out.push(`--impl must be one of ${IMPL_VALUES.join(", ")} (an unknown value used to select the improved extractor silently)`);
  if (!ONLY_VALUES.includes(only)) out.push(`--only must be one of ${ONLY_VALUES.join(", ")} (an unknown value used to select everything silently)`);
  if (!OCR_MODES.includes(ocrMode)) out.push(`--ocr must be one of ${OCR_MODES.join(", ")}`);
  if (labels !== undefined && !LABEL_MODES.includes(labels)) out.push(`--labels must be one of ${LABEL_MODES.join(", ")} (or omitted, to follow verification/label_policy.json)`);
  if (typeof label !== "string" || !LABEL_RE.test(label)) out.push("--label must be 1-81 characters of letters, digits, '.', '_' or '-', starting with a letter or digit (it names the files written under reports/)");
  return out;
}

/** Structural problems of a parsed held-out lock (pure). Same schema lock_heldout.mjs writes. */
export function lockProblems(lock) {
  if (!isObj(lock)) return ["the lock is not a JSON object"];
  const out = [];
  if (typeof lock.locked_at !== "string" || Number.isNaN(Date.parse(lock.locked_at))) out.push("locked_at is missing or not a timestamp");
  if (typeof lock.statement !== "string" || !lock.statement.trim()) out.push("statement is missing");
  if (!isObj(lock.code_sha256) || Object.keys(lock.code_sha256).length === 0 || !Object.values(lock.code_sha256).every((h) => typeof h === "string" && HEX64.test(h))) out.push("code_sha256 must be a non-empty object of 64-hex SHA-256 values");
  if (!Array.isArray(lock.heldout_receipts)) out.push("heldout_receipts is missing or not a list");
  else {
    const seen = new Set();
    lock.heldout_receipts.forEach((r, i) => {
      if (!isObj(r)) { out.push(`heldout_receipts[${i}] is not an object`); return; }
      if (typeof r.id !== "string" || !r.id.trim()) out.push(`heldout_receipts[${i}].id is missing or blank`);
      else if (seen.has(r.id)) out.push(`heldout_receipts lists receipt ${r.id.slice(0, 60)} more than once`);
      else seen.add(r.id);
      if (typeof r.sha256 !== "string" || !HEX64.test(r.sha256)) out.push(`heldout_receipts[${i}].sha256 is not 64 lowercase hex characters`);
      if (typeof r.family !== "string" || !r.family.trim()) out.push(`heldout_receipts[${i}].family is missing or blank`);
      if (typeof r.brand !== "string") out.push(`heldout_receipts[${i}].brand is missing`);
    });
  }
  return out;
}

/** Read intake/heldout_lock.json: { status: "absent" } | { status: "invalid", problem } | { status: "ok", lock }. Never throws. */
export function readHeldoutLock(fuelDir) {
  const f = path.join(fuelDir, "intake", "heldout_lock.json");
  let st;
  try { st = fs.lstatSync(f); } catch (e) { return e && e.code === "ENOENT" ? { status: "absent" } : { status: "invalid", problem: `intake/heldout_lock.json cannot be accessed (${e && e.code})` }; }
  if (!st.isFile()) return { status: "invalid", problem: "intake/heldout_lock.json is not a regular file" };
  let lock;
  try { lock = JSON.parse(fs.readFileSync(f, "utf8")); } catch { return { status: "invalid", problem: "intake/heldout_lock.json is unreadable or not valid JSON" }; }
  const p = lockProblems(lock);
  return p.length ? { status: "invalid", problem: `intake/heldout_lock.json is malformed: ${p.join("; ")}` } : { status: "ok", lock };
}

/** null if a corpus image (authentic / synthetic / control) is a regular, non-empty file inside <fuelDir>/corpus; otherwise the reason. Never reads bytes, never throws. */
export function corpusImageProblem(fuelDir, file) {
  try {
    const root = path.join(path.resolve(fuelDir), "corpus"), abs = path.resolve(file);
    if (!isInside(root, abs)) return "image unavailable (resolves outside corpus/)";
    let link;
    try { link = fs.lstatSync(abs); } catch (e) { return e && (e.code === "ENOENT" || e.code === "ENOTDIR") ? "image file not found" : "image unavailable (not accessible)"; }
    let realAbs, realRoot;
    try { realAbs = fs.realpathSync(abs); realRoot = fs.realpathSync(root); } catch { return link.isSymbolicLink() ? "image unavailable (broken symlink)" : "image unavailable (not accessible)"; }
    if (!isInside(realRoot, realAbs)) return "image unavailable (resolves outside corpus/)";
    const st = fs.statSync(realAbs);
    if (!st.isFile()) return "image unavailable (not a regular file)";
    if (st.size === 0) return "image unavailable (empty file)";
    return null;
  } catch { return "image unavailable (not accessible)"; }
}

/**
 * All problems that must stop an evaluation before OCR. `summary` / `eligibility` are what loadAnnotations returned; `selected` is the list of records the run will OCR,
 * [{ rec, group, file }] (file = the image path run_eval will read). Returns [] when the evaluation may start.
 */
export function evalPreflightProblems({ fuelDir, summary, eligibility, selected }) {
  const out = [];
  if (summary.intake_register_status === "invalid") out.push(`intake register is invalid: ${summary.intake_register_problem}`);
  for (const d of summary.skipped_intake_detail || []) out.push(`intake entry ${d.receipt_id}: ${d.reason} (the whole register must be valid for an evaluation)`);
  if (selected.length === 0) out.push("no eligible records were selected (check --only): there is nothing to evaluate, and an empty report would be misleading");

  let index;
  try { index = JSON.parse(fs.readFileSync(path.join(fuelDir, "corpus", "ocr-index.json"), "utf8")); } catch { index = null; }
  if (!isObj(index) || !isObj(index.images)) out.push("corpus/ocr-index.json is missing, unreadable or has no `images` object (it is read after the OCR loop for timing)");

  const usable = new Map(eligibility.intake.usable.map((e) => [e.receipt_id, e]));
  for (const { rec, file } of selected) {
    if (rec.image_path !== undefined) {
      const entry = usable.get(rec.receipt_id);
      const problem = entry ? intakeImageIntegrityProblem(fuelDir, entry) : "no usable intake-register entry for this record";
      if (problem) out.push(`receipt ${rec.receipt_id}: ${problem}`);
    } else {
      const problem = corpusImageProblem(fuelDir, file);
      if (problem) out.push(`receipt ${rec.receipt_id}: ${problem}`);
    }
  }

  const lockRead = readHeldoutLock(fuelDir);
  if (lockRead.status === "invalid") out.push(lockRead.problem);
  if (selected.some((s) => s.group === HELDOUT_INTAKE_GROUP)) {
    if (lockRead.status === "absent") out.push("held-out intake receipts are selected but there is no held-out lock (intake/heldout_lock.json): create it with scripts/lock_heldout.mjs before a held-out evaluation");
    else if (lockRead.status === "ok") {
      const lockById = new Map(lockRead.lock.heldout_receipts.map((r) => [r.id, r]));
      const registerHeld = eligibility.intake.usable.filter((e) => e.split === "heldout_untouched");
      const registerIds = new Set(registerHeld.map((e) => e.receipt_id));
      for (const e of registerHeld) {
        const l = lockById.get(e.receipt_id);
        if (!l) { out.push(`receipt ${e.receipt_id}: is a held-out entry in the register but is not in the held-out lock (registered after the lock was created?)`); continue; }
        if (l.sha256 !== e.sha256) out.push(`receipt ${e.receipt_id}: the register's sha256 differs from the sha256 recorded in the held-out lock (the register entry was changed after locking)`);
        if (l.family !== e.receipt_family) out.push(`receipt ${e.receipt_id}: the register's receipt_family differs from the one recorded in the held-out lock`);
        if (l.brand !== e.brand_family) out.push(`receipt ${e.receipt_id}: the register's brand_family differs from the one recorded in the held-out lock`);
      }
      for (const id of lockById.keys()) if (!registerIds.has(id)) out.push(`the held-out lock lists receipt ${id.slice(0, 80)}, which is not a usable held-out entry in the register`);
    }
  }
  return out;
}
