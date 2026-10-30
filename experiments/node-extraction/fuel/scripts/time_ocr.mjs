// Cold-cache timing of the OCR layer (single pass vs bounded retry) over the whole corpus, in a throwaway cache directory.
// Output: reports/timing.json  (per-mode totals / means / worst, number of images that needed the retry)
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { CachedOCR } from "../src/ocr.mjs";
import { extractFuelReceipt } from "../src/fuelExtractor.mjs";

const FUEL = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const annotations = JSON.parse(fs.readFileSync(path.join(FUEL, "corpus", "annotations.json"), "utf8")).records;
const dirOf = (r) => (r.source_type === "authentic" || r.source_type === "suspected_template_generated" ? "authentic" : r.split === "heldout2" ? "synthetic2" : r.split === "heldout3" ? "synthetic3" : "synthetic");
const out = { generated: new Date().toISOString(), hardware: { node: process.version, cpu: os.cpus()[0]?.model, cores: os.cpus().length, ramGB: Math.round(os.totalmem() / 2 ** 30), platform: os.platform() }, modes: {} };
for (const mode of ["single", "retry"]) {
  const cacheDir = fs.mkdtempSync(path.join(os.tmpdir(), `fuel-ocr-timing-${mode}-`));
  const ocr = new CachedOCR(cacheDir);
  const per = [];
  let extractMs = 0;
  for (const rec of annotations) {
    const p = path.join(FUEL, "corpus", dirOf(rec), "images", `${rec.receipt_id}.png`);
    const t0 = performance.now();
    const o = await ocr.read(p, { mode });
    const t1 = performance.now();
    extractFuelReceipt(o);
    extractMs += performance.now() - t1;
    per.push({ id: rec.receipt_id, ocr_ms: t1 - t0, attempts: o.attempts?.length ?? 1, retried: (o.attempts?.length ?? 1) > 1 });
  }
  await ocr.close();
  const ms = per.map((x) => x.ocr_ms).sort((a, b) => a - b);
  out.modes[mode] = { images: per.length, total_ocr_s: ms.reduce((a, b) => a + b, 0) / 1000, mean_ocr_ms: ms.reduce((a, b) => a + b, 0) / ms.length, median_ocr_ms: ms[Math.floor(ms.length / 2)], p95_ocr_ms: ms[Math.floor(ms.length * 0.95)], worst_ocr_ms: ms[ms.length - 1], retried_images: per.filter((x) => x.retried).length, extract_ms_mean: extractMs / per.length, note: "includes the one-off WASM worker start (~1s) in the first image" };
  fs.rmSync(cacheDir, { recursive: true, force: true });
  console.log(mode, JSON.stringify(out.modes[mode]));
}
fs.writeFileSync(path.join(FUEL, "reports", "timing.json"), JSON.stringify(out, null, 1));
