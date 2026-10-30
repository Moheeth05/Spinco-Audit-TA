// Locks the untouched held-out intake set to the CURRENT extractor and OCR code. Usage: node scripts/lock_heldout.mjs [--fuel-dir <dir>]
// run_eval compares the current code hashes with this lock; if they differ the held-out intake set can no longer be reported as untouched
// (any parser change made after the lock makes it development data, per the project rule).
//
// A lock is created ONLY if all of the following hold. Every check runs BEFORE anything is written; on any failure the command exits 1 and writes nothing.
//   * No intake/heldout_lock.json exists. An existing lock - valid, corrupt, empty, even a directory - is NEVER overwritten, repaired or deleted by this script
//     (it has no replacement or recovery procedure), and the lock file is created exclusively ("wx"), so two concurrent runs cannot overwrite each other.
//   * The intake register exists and is valid (src/intakeRegister.mjs): a readable JSON object with a `receipts` list in which EVERY entry is structurally usable - no
//     malformed entry, duplicate receipt_id / image_path / valid sha256, id colliding with corpus/annotations.json, or receipt_family under both splits. (The whole
//     register must be valid, not just the held-out part.)
//   * At least one usable entry has split "heldout_untouched" (an empty lock is refused: it would be meaningless and, being never overwritten, would block the real lock).
//   * Every held-out entry's image is AVAILABLE and its bytes match the recorded SHA-256 (intakeImageIntegrityProblem). Development entries' images are not checked.
// Lock schema and the code-integrity hashes (src/codeHash.mjs) are unchanged.
import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { hashCode } from "../src/codeHash.mjs";
import { readIntakeRegister, inspectIntakeEntries, intakeImageIntegrityProblem } from "../src/intakeRegister.mjs";

const argv = process.argv.slice(2);
const opt = {};
for (let i = 0; i < argv.length; i++) if (argv[i].startsWith("--")) opt[argv[i].slice(2)] = argv[++i];
const FUEL = path.resolve(opt["fuel-dir"] || path.join(path.dirname(fileURLToPath(import.meta.url)), ".."));
const lockFile = path.join(FUEL, "intake", "heldout_lock.json");

const MAX_LINES = 25;
const refuse = (lines) => {
  const all = [].concat(lines);
  const shown = all.length > MAX_LINES ? [...all.slice(0, MAX_LINES), `... and ${all.length - MAX_LINES} more`] : all;
  console.error("REFUSED - no lock was written:\n  " + shown.join("\n  "));
  process.exit(1);
};

// 1. an existing lock is never touched (lstat: also catches a dangling link or a directory)
let lockExists = false;
try { fs.lstatSync(lockFile); lockExists = true; } catch (e) { if (!e || e.code !== "ENOENT") refuse(`cannot check for an existing lock (${e && e.code}); refusing to risk overwriting one`); }
if (lockExists) refuse("a held-out lock already exists at intake/heldout_lock.json. An existing lock is never overwritten, repaired or deleted by this script (it was left untouched); this script has no replacement or recovery procedure.");

// 2. the register must exist and be valid
const reg = readIntakeRegister(FUEL);
if (reg.status === "absent") refuse("no intake register (intake/intake_register.json does not exist): there is nothing to lock");
if (reg.status === "invalid") refuse(`intake register is invalid: ${reg.problem}`);
let annotationIds;
try { annotationIds = new Set(JSON.parse(fs.readFileSync(path.join(FUEL, "corpus", "annotations.json"), "utf8")).records.map((r) => r.receipt_id)); } catch { refuse("corpus/annotations.json cannot be read, so id collisions between the register and the corpus cannot be ruled out"); }
const inspected = inspectIntakeEntries(reg.entries, annotationIds);
if (inspected.rejected.length) refuse(["the intake register has unusable entries - the whole register must be valid before a lock is created:", ...inspected.rejected.map((x) => `${x.receipt_id ?? "(no usable receipt_id)"}: ${x.reason}`)]);

// 3. the held-out entries: at least one, each with an available image whose bytes match the registered hash
const held = inspected.usable.filter((r) => r.split === "heldout_untouched");
if (!held.length) refuse("no eligible held-out entries (split heldout_untouched) in the register: an empty lock is never created");
const imageProblems = [];
for (const e of held) { const problem = intakeImageIntegrityProblem(FUEL, e); if (problem) imageProblems.push(`receipt ${e.receipt_id}: ${problem}`); }
if (imageProblems.length) refuse(imageProblems);

// 4. the code hashes
let codeHashes;
try { codeHashes = hashCode(FUEL); } catch { refuse("the code files that the lock hashes (src/codeHash.mjs CODE_FILES) cannot be read"); }

// 5. write (exclusive creation: an existing file - even one that appeared after the check above - is never replaced)
const lock = { locked_at: new Date().toISOString(), statement: "From this moment any change to the files below makes the listed receipts DEVELOPMENT data, not an untouched held-out set.", code_sha256: codeHashes, heldout_receipts: held.map((r) => ({ id: r.receipt_id, family: r.receipt_family, brand: r.brand_family, sha256: r.sha256 })) };
fs.mkdirSync(path.dirname(lockFile), { recursive: true });
try { fs.writeFileSync(lockFile, JSON.stringify(lock, null, 1), { flag: "wx" }); } catch (e) { refuse(e && e.code === "EEXIST" ? "a held-out lock appeared while this ran; it was not overwritten" : `the lock could not be written (${e && e.code})`); }
console.log(`locked ${held.length} held-out intake receipt(s) to the current extractor (images verified against their registered SHA-256)`);
