import fs from "node:fs"; import os from "node:os"; import path from "node:path";
import { CachedOCR } from "../src/ocr.mjs"; import { extractFuelReceipt } from "../src/fuelExtractor.mjs";
const FUEL = "D:/Bills/experiments/node-extraction/fuel";
const id = process.argv[2]; const sub = process.argv[3];
const ocr = new CachedOCR(fs.mkdtempSync(path.join(os.tmpdir(), "fuel-peek-")));
const p = `${FUEL}/corpus/${sub}/images/${id}.png`;
const rec = JSON.parse(fs.readFileSync(`${FUEL}/corpus/annotations.json`, "utf8")).records.find((r) => r.receipt_id === id);
for (const mode of ["single", "retry2"]) {
  const o = await ocr.read(p, { mode });
  const e = extractFuelReceipt(o);
  console.log(`\n=== ${mode}: chosen=${o.chosen_attempt} stopped=${o.retry_stopped} attempts=${(o.attempts || []).map((a) => a.name + ":" + a.confidence).join(", ")}`);
  console.log(o.lines.slice(0, 8).map((l) => `   ${(l.confidence ?? 0).toFixed(2)} ${l.text}`).join("\n"));
  for (const f of ["pump_name", "transaction_date", "total_amount"]) console.log(`  ${f}: got=${JSON.stringify(e[f].normalized_value)} review=${e[f].review_required} basis=${e[f].basis ?? "-"} | truth=${JSON.stringify(rec.fields[f].normalized_value)} | ${e[f].ambiguity ?? ""}`);
}
await ocr.close();
