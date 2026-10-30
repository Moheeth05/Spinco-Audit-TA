// Hashes of the code whose behaviour determines a held-out result (used by scripts/lock_heldout.mjs and scripts/run_eval.mjs).
import crypto from "node:crypto";
import fs from "node:fs";
import path from "node:path";

export const CODE_FILES = ["src/fuelExtractor.mjs", "src/ocr.mjs", "src/png.mjs", "src/scoring.mjs"];
export const hashCode = (dir) => Object.fromEntries(CODE_FILES.map((f) => [f, crypto.createHash("sha256").update(fs.readFileSync(path.join(dir, f))).digest("hex")]));
export function lockStatus(dir) {
  const f = path.join(dir, "intake", "heldout_lock.json");
  if (!fs.existsSync(f)) return { locked: false };
  const lock = JSON.parse(fs.readFileSync(f, "utf8"));
  const now = hashCode(dir);
  const changed = Object.keys(lock.code_sha256).filter((k) => lock.code_sha256[k] !== now[k]);
  return { locked: true, locked_at: lock.locked_at, still_untouched: changed.length === 0, changed_files: changed, receipts: lock.heldout_receipts.length };
}
