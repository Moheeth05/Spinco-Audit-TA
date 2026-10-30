// Strict reading and per-entry validation of intake/intake_register.json. That file is written ONLY by scripts/register_intake.mjs, which always
// writes { receipts: [ { receipt_id, image_path: "corpus/authentic_intake/pending/<id><ext>", provenance: { source_type: "authentic_issued_receipt", ... }, ... } ] }.
// Everything here is the loader's, the applier's and the review-pack generator's SINGLE definition of "a usable register entry", so the three can never disagree
// (the pack never offers what apply_verification.mjs would reject). Fail closed: anything that cannot be shown valid is not usable. Pure functions + one file read;
// messages never include entry contents other than the (length-limited) receipt id.
import crypto from "node:crypto";
import fs from "node:fs";
import path from "node:path";

export const INTAKE_IMAGE_PREFIX = "corpus/authentic_intake/";
export const AUTHENTIC_PROVENANCE = "authentic_issued_receipt";
const isPlainObject = (v) => v !== null && typeof v === "object" && !Array.isArray(v);
const textId = (e) => (isPlainObject(e) && typeof e.receipt_id === "string" && e.receipt_id.trim() !== "" ? e.receipt_id : null);

/** null if `p` is a plausible intake image path, otherwise the reason it is not. Existence on disk is checked by the caller that needs the file. */
export function imagePathProblem(p) {
  if (typeof p !== "string") return "image_path is missing or not text";
  if (p.trim() === "") return "image_path is blank";
  if (p.length > 300) return "image_path is unreasonably long";
  if (p !== p.trim() || /[\u0000-\u001f]/.test(p)) return "image_path has surrounding whitespace or control characters";
  if (p.includes("\\") || /^[A-Za-z]:/.test(p) || p.startsWith("/")) return "image_path must be a relative path written with forward slashes";
  if (p.split("/").some((s) => s === "" || s === "." || s === "..")) return "image_path has empty, '.' or '..' segments";
  if (!p.startsWith(INTAKE_IMAGE_PREFIX)) return `image_path must be inside ${INTAKE_IMAGE_PREFIX}`;
  return null;
}

// The two splits scripts/register_intake.mjs accepts (--split) and writes on every entry. Anything else is not a split (the loader used to map it silently to development).
export const INTAKE_SPLITS = ["development", "heldout_untouched"];
const SHA256_VALID = /^[0-9a-f]{64}$/;

/** Problems with ONE entry considered alone (empty list = structurally valid). */
export function entryProblems(e) {
  if (!isPlainObject(e)) return ["entry is not an object"];
  const out = [];
  if (typeof e.receipt_id !== "string" || e.receipt_id.trim() === "") out.push("receipt_id is missing, blank or not text");
  else if (e.receipt_id !== e.receipt_id.trim() || e.receipt_id.length > 120) out.push("receipt_id has surrounding whitespace or is too long");
  const ip = imagePathProblem(e.image_path);
  if (ip) out.push(ip);
  if (!isPlainObject(e.provenance)) out.push("provenance is missing or malformed");
  else if (e.provenance.source_type !== AUTHENTIC_PROVENANCE) out.push(`provenance.source_type is missing or not ${AUTHENTIC_PROVENANCE}`);
  // Both are REQUIRED by the writer (register_intake.mjs); without them the held-out rule ("a family goes wholly to one side") cannot be evaluated, so the entry is not usable.
  if (!INTAKE_SPLITS.includes(e.split)) out.push(`split is missing or not one of ${INTAKE_SPLITS.join(", ")}`);
  if (typeof e.receipt_family !== "string" || e.receipt_family.trim() === "") out.push("receipt_family is missing, blank or not text");
  return out;
}

// Comparison keys for the cross-entry checks. Only VALUES THAT CAN BE COMPARED RELIABLY take part: a path that passed imagePathProblem (case-folded, because NTFS paths are
// case-insensitive), a well-formed lowercase SHA-256, and a family name with a valid split (EXACT string equality - the same rule the writer applies; nothing is inferred from ids
// or names, so spelling variants of a family name are different families here exactly as they are to the writer).
const pathKey = (e) => (isPlainObject(e) && imagePathProblem(e.image_path) === null ? e.image_path.normalize("NFC").toLowerCase() : null);
const hashKey = (e) => (isPlainObject(e) && typeof e.sha256 === "string" && SHA256_VALID.test(e.sha256) ? e.sha256 : null);
const familyKey = (e) => (isPlainObject(e) && typeof e.receipt_family === "string" && e.receipt_family.trim() !== "" && INTAKE_SPLITS.includes(e.split) ? e.receipt_family : null);

/**
 * Classify register entries (PURE: no filesystem). `annotationIds`: ids of corpus/annotations.json records (a register id that collides with one is ambiguous and blocked).
 * Returns { usable: [entry], rejected: [{receipt_id|null, reason}], blocked: Map(id -> reason) }; every id that has any problem is blocked, so an export naming it
 * is refused. Beyond each entry's own problems, these cross-entry conflicts block ALL members of the conflicting group: a duplicate receipt_id, a duplicate image_path
 * (case-insensitive), a duplicate valid sha256, and a receipt_family that appears under both splits (exact string equality, as the writer compares it).
 */
export function inspectIntakeEntries(entries, annotationIds = new Set()) {
  const counts = new Map(), pathCounts = new Map(), hashCounts = new Map(), familySplits = new Map();
  const bump = (map, k) => { if (k !== null) map.set(k, (map.get(k) || 0) + 1); };
  for (const e of entries) {
    const id = textId(e); if (id) counts.set(id, (counts.get(id) || 0) + 1);
    bump(pathCounts, pathKey(e)); bump(hashCounts, hashKey(e));
    const fam = familyKey(e); if (fam !== null) { if (!familySplits.has(fam)) familySplits.set(fam, new Set()); familySplits.get(fam).add(e.split); }
  }
  const usable = [], rejected = [], blocked = new Map();
  for (const e of entries) {
    const problems = entryProblems(e);
    const id = textId(e);
    if (id && counts.get(id) > 1) problems.push("receipt_id appears more than once in the intake register");
    if (id && annotationIds.has(id)) problems.push("receipt_id collides with a record in corpus/annotations.json");
    // Cross-entry conflicts are counted over EVERY entry (even ones that are invalid for another reason), and they block EVERY member of the group: no entry can become
    // eligible by being first, by being the one with fewer problems, or by sitting on the "right" side.
    const pk = pathKey(e), hk = hashKey(e), fk = familyKey(e);
    if (pk !== null && pathCounts.get(pk) > 1) problems.push("image_path appears on more than one register entry (compared case-insensitively)");
    if (hk !== null && hashCounts.get(hk) > 1) problems.push("sha256 appears on more than one register entry (the same image registered twice)");
    if (fk !== null && familySplits.get(fk).size > 1) problems.push(`receipt_family '${fk.slice(0, 60)}' appears under both development and heldout_untouched in the register (a family must sit wholly on one split)`);
    if (problems.length) {
      const reason = `intake entry: ${problems.join("; ")}`;
      rejected.push({ receipt_id: id ? id.slice(0, 80) : null, reason });
      if (id) blocked.set(id, reason);
    } else usable.push(e);
  }
  return { usable, rejected, blocked };
}

const isInside = (dir, p) => { const rel = path.relative(dir, p); return rel === "" || (!rel.startsWith("..") && !path.isAbsolute(rel)); };

/**
 * IMAGE AVAILABILITY (a filesystem property, deliberately separate from the pure structural checks above - callers at an I/O boundary use it, the loader does not).
 * null if the entry's image is available, otherwise a short reason: the (structurally valid) path must resolve - symlinks, junctions and `..` included - to a
 * REGULAR, NON-EMPTY file that stays inside <fuelDir>/corpus/authentic_intake/. Never reads image bytes and never throws (missing / inaccessible / broken-link
 * paths all return a reason). Image INTEGRITY (the register's sha256 matching the bytes) is a different, stronger check and is not done here.
 */
export function intakeImageProblem(fuelDir, entry) {
  return resolveIntakeImage(fuelDir, entry).problem ?? null;
}

// Shared by the availability and integrity checks: { problem } or { file } where `file` is the REAL (symlink-resolved) path that passed every availability test.
function resolveIntakeImage(fuelDir, entry) {
  const p = isPlainObject(entry) ? entry.image_path : undefined;
  const structural = imagePathProblem(p);
  if (structural) return { problem: `image unavailable (${structural})` };
  try {
    const root = path.join(path.resolve(fuelDir), "corpus", "authentic_intake");
    const abs = path.resolve(fuelDir, p);
    if (!isInside(root, abs)) return { problem: "image unavailable (resolves outside corpus/authentic_intake/)" };
    let link;
    try { link = fs.lstatSync(abs); } catch (e) { return { problem: e && (e.code === "ENOENT" || e.code === "ENOTDIR") ? "image file not found" : "image unavailable (not accessible)" }; }
    let realAbs, realRoot;
    try { realAbs = fs.realpathSync(abs); realRoot = fs.realpathSync(root); } catch { return { problem: link.isSymbolicLink() ? "image unavailable (broken symlink)" : "image unavailable (not accessible)" }; }
    if (!isInside(realRoot, realAbs)) return { problem: "image unavailable (resolves outside corpus/authentic_intake/)" };
    const st = fs.statSync(realAbs);
    if (!st.isFile()) return { problem: "image unavailable (not a regular file)" };
    if (st.size === 0) return { problem: "image unavailable (empty file)" };
    return { file: realAbs };
  } catch { return { problem: "image unavailable (not accessible)" }; }
}

// The register's `sha256` contract, as written by scripts/register_intake.mjs: lowercase hex SHA-256 of the RAW bytes of the stored image (the stored copy is
// written from the very bytes that were hashed). No normalisation is applied here: a hash in any other form is malformed, never "fixed up".
function sha256OfFile(file) {
  const fd = fs.openSync(file, "r");
  try {
    const h = crypto.createHash("sha256"), buf = Buffer.allocUnsafe(1 << 20);
    for (let n = fs.readSync(fd, buf, 0, buf.length, null); n > 0; n = fs.readSync(fd, buf, 0, buf.length, null)) h.update(buf.subarray(0, n));
    return h.digest("hex");
  } finally { fs.closeSync(fd); }
}

/**
 * IMAGE INTEGRITY - the full boundary check used before an intake image is offered for review or a review of it is applied: AVAILABILITY first (exactly the reasons of
 * intakeImageProblem), then the register's `sha256` must be present, well formed and equal to the SHA-256 of the file's bytes. null = available AND intact; otherwise a
 * reason that never contains image content (only 12-character hash prefixes). The bytes are read only when every availability test passed, from the SAME resolved file
 * (so a link cannot be retargeted between the check and the read), in 1 MiB chunks. A missing or malformed hash is unverifiable and therefore refused (fail closed):
 * the only writer always emits a valid one. Never throws. Like availability, this belongs to the I/O boundaries - the loader and the structural validator never call it.
 */
export function intakeImageIntegrityProblem(fuelDir, entry) {
  const r = resolveIntakeImage(fuelDir, entry);
  if (r.problem) return r.problem;
  const expected = entry.sha256;
  if (expected === undefined || expected === null) return "image integrity unverifiable (sha256 is missing from the register entry)";
  if (typeof expected !== "string" || !SHA256_VALID.test(expected)) return "image integrity unverifiable (sha256 is not 64 lowercase hex characters)";
  let actual;
  try { actual = sha256OfFile(r.file); } catch { return "image integrity unverifiable (the image file could not be read)"; }
  return actual === expected ? null : `image integrity check failed (the image content does not match the registered sha256: registered ${expected.slice(0, 12)}..., actual ${actual.slice(0, 12)}...)`;
}

/**
 * Read the register file. status: "absent" (no file: no intake receipts), "ok" (entries = its `receipts` array), or "invalid" (cannot be trusted at all:
 * unreadable / not JSON / top level not an object / `receipts` missing or not a list). An invalid register never throws and yields no entries.
 */
export function readIntakeRegister(fuelDir) {
  const f = path.join(fuelDir, "intake", "intake_register.json");
  if (!fs.existsSync(f)) return { status: "absent", entries: [] };
  let raw;
  try { raw = JSON.parse(fs.readFileSync(f, "utf8")); } catch { return { status: "invalid", problem: "intake/intake_register.json is unreadable or not valid JSON", entries: [] }; }
  if (!isPlainObject(raw)) return { status: "invalid", problem: "intake/intake_register.json: the top level is not an object", entries: [] };
  if (!Array.isArray(raw.receipts)) return { status: "invalid", problem: "intake/intake_register.json: 'receipts' is missing or not a list", entries: [] };
  return { status: "ok", entries: raw.receipts };
}
