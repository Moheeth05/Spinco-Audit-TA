// OCR every corpus image once with local tesseract.js and cache the result (OCR output only; no predictions).
// Usage: node scripts/ocr_corpus.mjs
import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { CachedOCR } from "../src/ocr.mjs";

const HERE = path.dirname(fileURLToPath(import.meta.url));
const FUEL = path.resolve(HERE, "..");
const dirs = [path.join(FUEL, "corpus", "authentic", "images"), path.join(FUEL, "corpus", "synthetic", "images")];
const ocr = new CachedOCR(path.join(FUEL, "corpus", "ocr-cache"));
const index = {};
const t0 = Date.now();
let n = 0;
for (const d of dirs) {
  for (const f of fs.readdirSync(d).filter((x) => x.endsWith(".png")).sort()) {
    const id = f.replace(/\.png$/, "");
    const t1 = Date.now();
    const r = await ocr.read(path.join(d, f));
    index[id] = { image: path.relative(FUEL, path.join(d, f)).replace(/\\/g, "/"), ms: Date.now() - t1, cache: r.cache, confidence: r.confidence, chars: r.text.length, lines: r.lines.length, sha256: r.imageSha256 };
    n++;
    if (n % 10 === 0) console.log(`${n} images, ${((Date.now() - t0) / 1000).toFixed(0)}s`);
  }
}
await ocr.close();
fs.writeFileSync(path.join(FUEL, "corpus", "ocr-index.json"), JSON.stringify({ generated: new Date().toISOString(), engine: "tesseract.js@7 (local WASM), rotateAuto, lang=eng", totalMs: Date.now() - t0, stats: ocr.stats, images: index }, null, 1));
console.log(`done: ${n} images in ${((Date.now() - t0) / 1000).toFixed(0)}s; cache hits ${ocr.stats.hits}, OCR runs ${ocr.stats.misses}`);
