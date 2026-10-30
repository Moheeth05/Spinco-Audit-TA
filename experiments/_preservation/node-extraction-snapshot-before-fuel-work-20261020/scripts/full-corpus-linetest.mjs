// Runs the real-doc line-overlap comparison across every PDF in the
// manifest, tagged by its dev/heldout role, and prints a summary.
import { execFileSync } from "node:child_process";
import { readFile } from "node:fs/promises";
import { PdfJsTextExtractor } from "../src/textExtraction.mjs";

const PYTHON = "D:\\Bills\\backend\\.venv\\Scripts\\python.exe";
const manifest = JSON.parse(await readFile("datasets/manifest.json", "utf-8"));

function pyContentPages(pdfPath) {
  const script = `
import sys, json
sys.path.insert(0, r"D:\\Bills\\backend")
from pathlib import Path
from app.services.extraction.text_extraction import PyMuPDFTextExtractor
extractor = PyMuPDFTextExtractor(ocr_engine=None)
pages = extractor.content_pages(Path(r"${pdfPath}"))
print(json.dumps([{"text": p["text"], "source": p["source"]} for p in pages]))
`;
  return JSON.parse(execFileSync(PYTHON, ["-I", "-c", script], { encoding: "utf-8", maxBuffer: 50 * 1024 * 1024 }));
}

const results = [];
for (const report of manifest.reportsCovered) {
  const pdfPath = `fixtures/reference-pdfs/${report.filename}`;
  const extractor = new PdfJsTextExtractor(null);
  const jsPages = await extractor.contentPages(pdfPath);
  const pyPages = pyContentPages(pdfPath);

  let totalJsLines = 0, totalPyLines = 0, totalOverlap = 0;
  const pageResults = [];
  for (let i = 0; i < Math.max(jsPages.length, pyPages.length); i++) {
    // Only compare pages Python also treats as native (no OCR involved -
    // OCR text parity is a separate, already-flagged concern, not what
    // this reconstruction fix targets).
    if (pyPages[i] && pyPages[i].source !== "native") continue;
    const jsLines = (jsPages[i]?.text || "").split("\n").map((l) => l.trim()).filter(Boolean);
    const pyLines = (pyPages[i]?.text || "").split("\n").map((l) => l.trim()).filter(Boolean);
    const jsSet = new Set(jsLines);
    const overlap = pyLines.filter((l) => jsSet.has(l)).length;
    totalJsLines += jsLines.length;
    totalPyLines += pyLines.length;
    totalOverlap += overlap;
    pageResults.push({ page: i + 1, jsLines: jsLines.length, pyLines: pyLines.length, overlap });
  }
  results.push({ filename: report.filename, role: report.role, totalJsLines, totalPyLines, totalOverlap, pageResults });
  const pct = totalPyLines ? ((totalOverlap / totalPyLines) * 100).toFixed(1) : "n/a";
  console.log(`[${report.role}] ${report.filename}: ${totalOverlap}/${totalPyLines} lines match (${pct}%) across ${pageResults.length} native content pages`);
  for (const p of pageResults) {
    if (p.overlap !== p.pyLines) {
      console.log(`    page ${p.page}: JS=${p.jsLines} Py=${p.pyLines} overlap=${p.overlap} <-- MISMATCH`);
    }
  }
}

await (await import("node:fs/promises")).writeFile("reports/line-overlap-results.json", JSON.stringify(results, null, 2));
console.log("\nWrote reports/line-overlap-results.json");
