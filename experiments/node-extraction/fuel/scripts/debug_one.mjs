// DEV-ONLY: dump the extractor output for one DEV receipt id. Refuses held-out/control ids.
import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { CachedOCR } from "../src/ocr.mjs";
import { extractFuelReceipt } from "../src/fuelExtractor.mjs";

const FUEL = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const id = process.argv[2];
const rec = JSON.parse(fs.readFileSync(path.join(FUEL, "corpus", "annotations.json"), "utf8")).records.find((r) => r.receipt_id === id);
if (!rec || rec.split !== "dev") throw new Error(`${id}: not a development receipt - refusing`);
const ocr = new CachedOCR(path.join(FUEL, "corpus", "ocr-cache"));
const o = await ocr.read(path.join(FUEL, "corpus", rec.source_type === "authentic" ? "authentic" : "synthetic", "images", `${id}.png`), { mode: "retry" });
o.lines.slice(0, Number(process.argv[3] || 14)).forEach((l, i) => console.log(i, (l.confidence ?? 0).toFixed(2), JSON.stringify(l.text), l.words.map((w) => `${w.text}:${(w.confidence ?? 0).toFixed(2)}`).join(" ")));
console.log(JSON.stringify(extractFuelReceipt(o).pump_name, null, 1));
await ocr.close();

