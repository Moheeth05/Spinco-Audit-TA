// Builds a throw-away MIRROR of the review workflow: copies only the scripts/modules the workflow needs into a fresh directory and fills it with
// SYNTHETIC fixtures (blank 1x1 images, invented dealers, test-only policy). The copied scripts derive their data directory from their OWN location
// (import.meta.url), so running them from the mirror reads and writes only the mirror - never the real corpus, review pack, labels or policy.
// `base` defaults to the OS temp dir; set FUEL_SYNTHETIC_E2E_ROOT to keep mirrors inside a chosen folder. Mirrors are never deleted here.
import crypto from "node:crypto";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import { spawnSync } from "node:child_process";
import { fileURLToPath } from "node:url";

export const REAL_FUEL = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..", "..");
export const PNG_1X1 = Buffer.from("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg==", "base64");
/** The register's `sha256` contract (scripts/register_intake.mjs): lowercase hex SHA-256 of the raw bytes of the stored image. This is that value for the blank placeholder image the mirrors create. */
export const sha256Hex = (bytes) => crypto.createHash("sha256").update(bytes).digest("hex");
export const PLACEHOLDER_SHA256 = sha256Hex(PNG_1X1);
/** Distinct placeholder image bytes per receipt id (a real register never holds two entries for identical bytes), and the matching register hash. */
export const placeholderImageBytes = (id) => Buffer.concat([PNG_1X1, Buffer.from(`|synthetic-placeholder|${String(id)}`)]);
export const placeholderSha256 = (id) => sha256Hex(placeholderImageBytes(id));
/** A structurally valid synthetic intake register entry whose recorded sha256 matches the placeholder image `placeIntakeImages` writes. */
export const intakeEntry = (id, o = {}) => ({
  receipt_id: id, image_path: `corpus/authentic_intake/pending/${id}.png`, sha256: placeholderSha256(id), registered_at: "2026-10-22T00:00:00Z", split: "development",
  receipt_family: "synthetic_family_a", brand_family: "other", label_status: "pending_human_label",
  provenance: { source_type: "authentic_issued_receipt", source_description: "synthetic fixture description" }, ...o,
});
/** sha256 of every file under a mirror except in/ (where applyExports drops its input): proves that a rejected invocation wrote NOTHING. Links are recorded, not followed. */
export function treeState(root) {
  const out = {};
  const walk = (d) => {
    for (const e of fs.readdirSync(d, { withFileTypes: true })) {
      const p = path.join(d, e.name), rel = path.relative(root, p);
      if (rel.split(path.sep)[0] === "in") continue;
      if (e.isSymbolicLink()) out[rel] = "link";
      else if (e.isDirectory()) { out[`${rel}/`] = "dir"; walk(p); } else out[rel] = sha256Hex(fs.readFileSync(p));
    }
  };
  walk(root);
  return out;
}
export const COPIED = ["scripts/build_review_pack.mjs", "scripts/apply_verification.mjs", "scripts/lock_heldout.mjs", "scripts/run_eval.mjs", "src/labels.mjs", "src/reviewValidation.mjs", "src/intakeRegister.mjs", "src/evalPreflight.mjs", "src/codeHash.mjs"];
// The four code files the held-out lock hashes (src/codeHash.mjs CODE_FILES) exist in a mirror only as DUMMY stand-ins - never the real extractor/OCR code.
const MIRROR_CODE_FILES = ["src/fuelExtractor.mjs", "src/ocr.mjs", "src/png.mjs", "src/scoring.mjs"];

export const rec = (id, source_type, status, pump, date, amt, family = "synthetic_fixture_family") => ({
  receipt_id: id, source_type, receipt_family: family, split: source_type === "authentic" ? "dev" : "heldout", kind: "fuel", annotation_status: status,
  fields: {
    pump_name: { raw_value: pump, normalized_value: pump, acceptable_forms: [pump], review_required: false },
    transaction_date: { raw_value: date, normalized_value: date, review_required: false, ambiguous_on_receipt_alone: false },
    total_amount: { raw_value: String(amt), normalized_value: amt, review_required: false },
  },
});

export function defaultRecords() {
  return [
    // TEST-ONLY stand-ins for the human-verifiable classes (nothing here is a real receipt)
    rec("SYNTH-AUTH-001", "authentic", "provisional_assistant_read", "SYNTHETIC DEALER ONE", "2026-09-24", 100),
    rec("SYNTH-AUTH-002", "authentic", "provisional_assistant_read", "SYNTHETIC DEALER TWO", "2026-08-25", 200),
    rec("SYNTH-TPL-001", "suspected_template_generated", "provisional_assistant_read", "SYNTHETIC TEMPLATE DEALER", "2026-07-15", 300),
    // construction-truth classes that must never become human-verified
    rec("SYNTH-FUEL-001", "synthetic", "construction_truth", "SYNTHETIC PUMP", "2026-06-01", 500),
    rec("SYNTH-CTRL-001", "generic_control", "construction_truth", "SYNTHETIC SHOP", "2026-05-01", 50),
  ];
}

/** Blank placeholder image for every plausible entry of an intake-register object (unless omitted), written only where the path provably stays inside the mirror's intake image folder. */
export function placeIntakeImages(root, intake, omit = []) {
  for (const e of Array.isArray(intake && intake.receipts) ? intake.receipts : []) {
    const p = e && e.image_path;
    if (typeof p !== "string" || !p.startsWith("corpus/authentic_intake/") || p.length > 150 || p.endsWith("/") || /[\u0000-\u001f\\:]/.test(p) || p.includes("..") || omit.includes(e.receipt_id)) continue;
    const dest = path.resolve(root, p);
    if (!path.relative(path.join(root, "corpus", "authentic_intake"), dest).startsWith("..")) { fs.mkdirSync(path.dirname(dest), { recursive: true }); fs.writeFileSync(dest, placeholderImageBytes(e.receipt_id)); }
  }
}

/** Creates a fresh mirror tree and returns its paths. `extraRecords` are appended to the synthetic annotations. */
export function makeMirror({ base = process.env.FUEL_SYNTHETIC_E2E_ROOT || os.tmpdir(), extraRecords = [], intake = null, policy = null, omitIntakeImages = [] } = {}) {
  fs.mkdirSync(base, { recursive: true });
  const root = fs.mkdtempSync(path.join(base, "synthetic-mirror-"));
  for (const d of ["scripts", "src", "corpus/authentic/images", "verification", "in", "intake"]) fs.mkdirSync(path.join(root, d), { recursive: true });
  for (const f of COPIED) fs.copyFileSync(path.join(REAL_FUEL, f), path.join(root, f));
  for (const f of MIRROR_CODE_FILES) fs.writeFileSync(path.join(root, f), `// DUMMY synthetic stand-in for ${f} (mirror only)\n`);
  const records = [...defaultRecords(), ...extraRecords];
  fs.writeFileSync(path.join(root, "corpus", "annotations.json"), JSON.stringify({ note: "SYNTHETIC TEST FIXTURES ONLY", records }, null, 1));
  for (const r of records) if (r.source_type === "authentic" || r.source_type === "suspected_template_generated") fs.writeFileSync(path.join(root, "corpus", "authentic", "images", `${r.receipt_id}.png`), PNG_1X1);
  fs.writeFileSync(path.join(root, "verification", "label_policy.json"), JSON.stringify(policy || { assistant_read_labels: "provisional_included", note: "TEST-ONLY synthetic mirror policy" }, null, 1));
  if (intake) {
    fs.writeFileSync(path.join(root, "intake", "intake_register.json"), JSON.stringify(intake, null, 1));
    placeIntakeImages(root, intake, omitIntakeImages);
  }
  return { root, scripts: path.join(root, "scripts"), verifiedFile: path.join(root, "corpus", "verified_labels.json"), packDir: path.join(root, "review-pack") };
}

/** Throws unless every data path the copied scripts will use provably resolves inside the synthetic mirror. */
export function assertIsolated(root) {
  const r = path.resolve(root), real = path.resolve(REAL_FUEL);
  const inside = (p, dir) => { const rel = path.relative(dir, p); return rel === "" || (!rel.startsWith("..") && !path.isAbsolute(rel)); };
  if (r === real) throw new Error("mirror root is the real fuel directory");
  for (const d of ["corpus", "verification", "intake", "reports", "src", "scripts"]) if (inside(r, path.join(real, d))) throw new Error(`mirror root is inside the real ${d}/ directory`);
  for (const f of COPIED) {
    const text = fs.readFileSync(path.join(r, f), "utf8");
    if (text.includes(real) || /D:\\\\?Bills/i.test(text)) throw new Error(`${f} contains an absolute path to the real tree`);
    if (/scripts\//.test(f) && !text.includes("import.meta.url")) throw new Error(`${f} does not derive its data directory from its own location`);
  }
  const ann = JSON.parse(fs.readFileSync(path.join(r, "corpus", "annotations.json"), "utf8")).records;
  const bad = ann.filter((x) => !String(x.receipt_id).startsWith("SYNTH-") && !String(x.receipt_id).startsWith("SYNTH_")).map((x) => x.receipt_id);
  if (bad.length) throw new Error(`non-synthetic ids in the mirror annotations: ${bad.join(", ")}`);
  if (fs.existsSync(path.join(r, "intake", "intake_register.json"))) {
    let reg = [];
    try { const raw = JSON.parse(fs.readFileSync(path.join(r, "intake", "intake_register.json"), "utf8")); reg = Array.isArray(raw && raw.receipts) ? raw.receipts : []; } catch { reg = []; } // a deliberately malformed register has no ids to check
    if (reg.some((x) => x && typeof x === "object" && typeof x.receipt_id === "string" && !x.receipt_id.startsWith("SYNTH-"))) throw new Error("non-synthetic ids in the mirror intake register");
  }
  return true;
}

/** Write raw text (possibly invalid JSON) as the mirror's intake register, after proving the target is inside the mirror. */
export function writeRegisterRaw(m, text) {
  const reg = path.resolve(m.root, "intake", "intake_register.json");
  if (path.relative(m.root, reg).startsWith("..") || path.isAbsolute(path.relative(m.root, reg))) throw new Error("register path is outside the mirror");
  fs.mkdirSync(path.dirname(reg), { recursive: true });
  fs.writeFileSync(reg, text);
  return reg;
}
/** Write a register object into the mirror (inside-mirror checked) and give its plausible entries blank placeholder images, except the ids in `omit`. */
export function writeRegister(m, obj, omit = []) {
  const reg = writeRegisterRaw(m, JSON.stringify(obj, null, 1));
  placeIntakeImages(m.root, obj, omit);
  return reg;
}
export const buildPack = (m, args = []) => runScript(m.root, "build_review_pack.mjs", ["--out", m.packDir, ...args]);

export const runScript = (root, script, args = []) => {
  const x = spawnSync("node", [path.join(root, "scripts", script), ...args], { cwd: root, encoding: "utf8" });
  return { status: x.status, out: x.stdout, err: x.stderr, all: x.stdout + x.stderr };
};
let n = 0;
export function applyExports(root, exports_) {
  const files = exports_.map((e) => { const f = path.join(root, "in", `export-${++n}.json`); fs.writeFileSync(f, JSON.stringify(e)); return f; });
  return runScript(root, "apply_verification.mjs", files);
}
export const goodEntry = (o = {}) => ({
  pump_name: { status: "read", value: "SYNTHETIC DEALER ONE", printed_text: "SYNTHETIC DEALER ONE" },
  transaction_date: { status: "read", printed_text: "24/09/26", iso_date: "2026-09-24", order_provable_from_receipt: true, order_evidence: "component_gt_12" },
  total_amount: { status: "read", printed_text: "100.00", value: 100 }, ...o,
});
export const exportFile = (reviewer_id, records, extra = {}) => ({ schema: "fuel-review-export/v1", reviewer_id, authorization_confirmed: true, exported_at: "2026-10-22T10:00:00Z", records, ...extra });
export const readJson = (f) => JSON.parse(fs.readFileSync(f, "utf8"));
