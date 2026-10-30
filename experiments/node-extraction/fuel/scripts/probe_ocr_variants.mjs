// Probe OCR variants on consumed receipts: which settings recover text? (diagnostic only; nothing cached, nothing scored)
import { createWorker, PSM } from "tesseract.js";
import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

const FUEL = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const ids = process.argv.slice(2);
const worker = await createWorker("eng");
const variants = [
  ["default+rotateAuto", {}, { rotateAuto: true }],
  ["no rotateAuto", {}, {}],
  ["psm6 (uniform block)", { tessedit_pageseg_mode: PSM.SINGLE_BLOCK }, {}],
  ["psm4 (single column)", { tessedit_pageseg_mode: PSM.SINGLE_COLUMN }, {}],
  ["rotate +90", {}, { rotateRadians: Math.PI / 2 }],
  ["rotate -90", {}, { rotateRadians: -Math.PI / 2 }],
];
for (const id of ids) {
  const dir = id.startsWith("auth_") ? "authentic" : "synthetic";
  const img = fs.readFileSync(path.join(FUEL, "corpus", dir, "images", `${id}.png`));
  console.log(`\n##### ${id}`);
  for (const [name, params, opts] of variants) {
    await worker.setParameters({ tessedit_pageseg_mode: PSM.AUTO, ...params });
    const t0 = Date.now();
    const { data } = await worker.recognize(img, opts, { text: true });
    const text = data.text || "";
    const hits = (text.match(/PETROL|DIESEL|AMOUNT|DATE|SALE|NOZZLE|VOLUME|RATE/gi) || []).length;
    console.log(`  ${name.padEnd(22)} conf=${data.confidence} chars=${text.length} keywordHits=${hits} ${Date.now() - t0}ms | ${text.replace(/\s+/g, " ").slice(0, 90)}`);
  }
}
await worker.terminate();
