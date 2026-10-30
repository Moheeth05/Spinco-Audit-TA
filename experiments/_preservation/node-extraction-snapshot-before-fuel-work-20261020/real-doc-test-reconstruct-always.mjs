import { execFileSync } from "node:child_process";
import { readFile } from "node:fs/promises";
import { getDocument } from "pdfjs-dist/legacy/build/pdf.mjs";

const REAL_PDF = "D:\\Bills\\uat-data-test\\documents\\expense_reports\\506d7dbf9383_reahul report.pdf";
const PYTHON = "D:\\Bills\\backend\\.venv\\Scripts\\python.exe";

function reconstructFromItems(items) {
  const spans = items.filter((it) => it.str && it.str.trim()).map((it) => ({ x0: it.transform[4], y0: -it.transform[5], text: it.str }));
  if (!spans.length) return "";
  spans.sort((a, b) => (a.y0 - b.y0) || (a.x0 - b.x0));
  const yTolerance = 3.0;
  const rows = [];
  let rowY = null;
  for (const s of spans) {
    if (rows.length && rowY !== null && Math.abs(s.y0 - rowY) <= yTolerance) rows[rows.length - 1].push(s);
    else { rows.push([s]); rowY = s.y0; }
  }
  return rows.map((row) => row.sort((a, b) => a.x0 - b.x0).map((s) => s.text).join(" ")).join("\n");
}

const data = await readFile(REAL_PDF);
const doc = await getDocument({ data: new Uint8Array(data), useSystemFonts: true }).promise;
const page1 = await doc.getPage(1);
const content = await page1.getTextContent();
const reconstructed = reconstructFromItems(content.items);
await doc.destroy();

const pyText = JSON.parse(execFileSync(PYTHON, ["-I", "-c", `
import sys, json
sys.path.insert(0, r"D:\\Bills\\backend")
from pathlib import Path
from app.services.extraction.text_extraction import PyMuPDFTextExtractor
e = PyMuPDFTextExtractor(ocr_engine=None)
pages = e.content_pages(Path(r"${REAL_PDF}"))
print(json.dumps(pages[0]["text"]))
`], { encoding: "utf-8", maxBuffer: 20 * 1024 * 1024 }));

const jsLines = reconstructed.split("\n").map((l) => l.trim()).filter(Boolean);
const pyLines = pyText.split("\n").map((l) => l.trim()).filter(Boolean);
const jsSet = new Set(jsLines);
const matching = pyLines.filter((l) => jsSet.has(l)).length;
console.log(`Always-reconstruct: JS lines=${jsLines.length} Python lines=${pyLines.length}`);
console.log(`Exact-line overlap: ${matching}/${pyLines.length}`);
if (matching < pyLines.length) {
  console.log(`Missing (first 10): ${JSON.stringify(pyLines.filter((l) => !jsSet.has(l)).slice(0, 10))}`);
}
console.log(`\nFirst 15 JS reconstructed lines:\n${jsLines.slice(0, 15).join("\n")}`);
