import { execFileSync } from "node:child_process";
import { PdfJsTextExtractor } from "../src/textExtraction.mjs";

const PDF = process.argv[2] || "fixtures/reference-pdfs/reahul-report-ER40513.pdf";
const PAGE_IDX = parseInt(process.argv[3] || "0", 10);
const PYTHON = "D:\\Bills\\backend\\.venv\\Scripts\\python.exe";

function pyContentPages() {
  const script = `
import sys, json
sys.path.insert(0, r"D:\\Bills\\backend")
from pathlib import Path
from app.services.extraction.text_extraction import PyMuPDFTextExtractor
extractor = PyMuPDFTextExtractor(ocr_engine=None)
pages = extractor.content_pages(Path(r"${PDF}"))
print(json.dumps([p["text"] for p in pages]))
`;
  return JSON.parse(execFileSync(PYTHON, ["-I", "-c", script], { encoding: "utf-8", maxBuffer: 20 * 1024 * 1024 }));
}

const extractor = new PdfJsTextExtractor(null);
const jsPages = await extractor.contentPages(PDF);
const pyPages = pyContentPages();

const jsLines = (jsPages[PAGE_IDX]?.text || "").split("\n").map((l) => l.trim()).filter(Boolean);
const pyLines = (pyPages[PAGE_IDX] || "").split("\n").map((l) => l.trim()).filter(Boolean);

console.log(`=== JS (${jsLines.length}) ===`);
jsLines.forEach((l, i) => console.log(`${i}: ${JSON.stringify(l)}`));
console.log(`\n=== Python (${pyLines.length}) ===`);
pyLines.forEach((l, i) => console.log(`${i}: ${JSON.stringify(l)}`));
