// A synthetic mirror in which the COPIED scripts/run_eval.mjs can run WITHOUT real OCR: the mirror's src/ocr.mjs and src/fuelExtractor.mjs are replaced by stubs, and
// the real (pure) src/scoring.mjs is copied. The stub OCR appends a line to <mirror>/ocr_stub_calls.log when it is constructed and for every image it is asked to read, so a
// test can PROVE that OCR never started (no log file at all). No real receipt, corpus image, tesseract worker or extractor code is involved; images are placeholders.
import fs from "node:fs";
import path from "node:path";
import { makeMirror, REAL_FUEL, placeholderImageBytes, runScript } from "./syntheticMirror.mjs";

const STUB_OCR = `// STUB OCR (synthetic mirror only): never recognises anything, only records that it was used.
import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";
const LOG = path.join(path.resolve(path.dirname(fileURLToPath(import.meta.url)), ".."), "ocr_stub_calls.log");
export class CachedOCR {
  constructor(cacheDir) { fs.appendFileSync(LOG, "construct\\n"); this.cacheDir = cacheDir; this.stats = { hits: 0, misses: 0, ocrMs: 0, retriedImages: 0 }; }
  async read(imagePath, { mode = "single" } = {}) { fs.appendFileSync(LOG, "read " + path.basename(imagePath) + " " + mode + "\\n"); return { text: "SYNTHETIC", confidence: 0.9, lines: [], stub_image: path.basename(imagePath) }; }
  async close() {}
}
`;
// STUB extractor: deterministic outputs keyed by the image name (SYNTH-AUTH-001 fully right and "auto-usable", SYNTH-AUTH-002 right except a confidently WRONG amount,
// everything else abstains and asks for review) so the report numbers are known in advance.
const STUB_EXTRACTOR = `// STUB extractor (synthetic mirror only).
export function extractFuelReceipt(ocr) {
  const id = String(ocr.stub_image || "").replace(/\\.png$/, "");
  const f = (v, review) => ({ normalized_value: v, review_required: review });
  if (id === "SYNTH-AUTH-001") return { pump_name: f("SYNTHETIC DEALER ONE", false), transaction_date: f("2026-09-24", false), total_amount: f(100, false), overall_review_required: false };
  if (id === "SYNTH-AUTH-002") return { pump_name: f("SYNTHETIC DEALER TWO", false), transaction_date: f("2026-08-25", false), total_amount: f(999, false), overall_review_required: false };
  return { pump_name: f(null, true), transaction_date: f(null, true), total_amount: f(null, true), overall_review_required: true };
}
`;

export function makeEvalMirror(opts = {}) {
  const m = makeMirror(opts);
  const root = m.root;
  fs.copyFileSync(path.join(REAL_FUEL, "src", "scoring.mjs"), path.join(root, "src", "scoring.mjs")); // the real, pure scoring code
  fs.writeFileSync(path.join(root, "src", "ocr.mjs"), STUB_OCR);
  fs.writeFileSync(path.join(root, "src", "fuelExtractor.mjs"), STUB_EXTRACTOR);
  fs.writeFileSync(path.join(root, "corpus", "ocr-index.json"), JSON.stringify({ note: "SYNTHETIC FIXTURE", images: {} }));
  fs.mkdirSync(path.join(root, "corpus", "synthetic", "images"), { recursive: true }); // images of the synthetic / control records run_eval looks for there
  for (const id of ["SYNTH-FUEL-001", "SYNTH-CTRL-001"]) fs.writeFileSync(path.join(root, "corpus", "synthetic", "images", `${id}.png`), placeholderImageBytes(id));
  return { ...m, reports: path.join(root, "reports"), ocrLog: path.join(root, "ocr_stub_calls.log") };
}
export const runEval = (m, args = []) => runScript(m.root, "run_eval.mjs", args);
export const corpusImage = (m, id, dir = "authentic") => path.join(m.root, "corpus", dir, "images", `${id}.png`);
