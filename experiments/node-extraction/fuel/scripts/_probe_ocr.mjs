// Probe: what does tesseract.js 7 return (text only, or lines/words with bboxes)? Throwaway diagnostic.
import { createWorker } from "tesseract.js";
import fs from "node:fs";

const img = fs.readFileSync(process.argv[2]);
const t0 = Date.now();
const worker = await createWorker("eng");
console.log("worker ready ms", Date.now() - t0);
const t1 = Date.now();
const res = await worker.recognize(img, { rotateAuto: true }, { text: true, blocks: true, tsv: true });
console.log("recognize ms", Date.now() - t1);
const d = res.data;
console.log("keys", Object.keys(d));
console.log("confidence", d.confidence, "text chars", (d.text || "").length);
console.log("blocks", Array.isArray(d.blocks) ? d.blocks.length : d.blocks);
const b0 = d.blocks?.[0];
if (b0) {
  console.log("block0 keys", Object.keys(b0));
  const p = b0.paragraphs?.[0];
  const l = p?.lines?.[0];
  console.log("line0", l && { text: l.text, bbox: l.bbox, conf: l.confidence, words: l.words?.slice(0, 3).map((w) => ({ t: w.text, bbox: w.bbox, c: w.confidence })) });
}
console.log("rotateAuto fields:", d.rotateRadians, d.orientation_degrees, d.rotate);
console.log((d.text || "").slice(0, 400));
await worker.terminate();
