// Proves the OCR + extractor make NO network request: fetch, http.request/get and https.request/get are replaced by functions that throw
// (and count), then a few images are OCR'd with a FRESH cache (so tesseract.js really runs) and compared with the cached results.
import fs from "node:fs";
import http from "node:http";
import https from "node:https";
import os from "node:os";
import path from "node:path";
import { fileURLToPath } from "node:url";

let attempts = 0;
const block = (name) => (...a) => { attempts++; throw new Error(`NETWORK BLOCKED: ${name}(${String(a[0]).slice(0, 80)})`); };
globalThis.fetch = block("fetch");
for (const m of [http, https]) { m.request = block(`${m === http ? "http" : "https"}.request`); m.get = block(`${m === http ? "http" : "https"}.get`); }

const { CachedOCR } = await import("../src/ocr.mjs");
const { extractFuelReceipt } = await import("../src/fuelExtractor.mjs");
const FUEL = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const ids = [["authentic", "auth_ER-39832_p04"], ["authentic", "auth_ER-40513_p24"], ["synthetic", "syn_dev_iocl_thermal_0"], ["synthetic", "syn_dev_iocl_thermal_5"]];
const fresh = new CachedOCR(fs.mkdtempSync(path.join(os.tmpdir(), "fuel-offline-")));
const cached = new CachedOCR(path.join(FUEL, "corpus", "ocr-cache"));
let same = 0;
for (const [dir, id] of ids) {
  const img = path.join(FUEL, "corpus", dir, "images", `${id}.png`);
  const a = await fresh.read(img, { mode: "retry" });
  const b = await cached.read(img, { mode: "retry" });
  const pa = extractFuelReceipt(a), pb = extractFuelReceipt(b);
  const eq = JSON.stringify([pa.pump_name.normalized_value, pa.transaction_date.normalized_value, pa.total_amount.normalized_value]) === JSON.stringify([pb.pump_name.normalized_value, pb.transaction_date.normalized_value, pb.total_amount.normalized_value]);
  same += eq ? 1 : 0;
  console.log(`${id}: fresh OCR (${fresh.stats.misses} runs so far) -> same three-field result as the cached run: ${eq}`);
}
await fresh.close(); await cached.close();
console.log(`network attempts caught: ${attempts}; identical results: ${same}/${ids.length}`);
if (attempts > 0) process.exit(1);
