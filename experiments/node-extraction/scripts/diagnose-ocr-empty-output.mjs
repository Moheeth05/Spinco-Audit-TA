// Step 1-2: render the SAME page (vivek-bhai-report-ER41220.pdf page 6)
// via both pipelines' own rendering code, save both PNGs locally, record
// dimensions, then cross-test both OCR engines against BOTH images - this
// isolates whether the difference is in RENDERING or in the OCR ENGINE
// itself, rather than assuming either.
import { writeFile } from "node:fs/promises";
import { execFileSync } from "node:child_process";
import { PdfJsTextExtractor } from "../src/textExtraction.mjs";
import { TesseractJsOCREngine } from "../src/ocrEngine.mjs";
import { getDocument } from "pdfjs-dist/legacy/build/pdf.mjs";
import { createCanvas } from "@napi-rs/canvas";
import { readFile } from "node:fs/promises";

const PDF = "fixtures/reference-pdfs/vivek-bhai-report-ER41220.pdf";
const PAGE = 6;
const OUT_DIR = "reports/ocr-diagnostic-page6";

await (await import("node:fs/promises")).mkdir(OUT_DIR, { recursive: true });

// --- Render via Node's own pipeline code (pdfjs-dist + @napi-rs/canvas, dpi=200) ---
// Dimensions read directly from the canvas object itself (not a third-party
// image-metadata library - none was previously a dependency of this
// experiment, and none is added here).
const extractor = new PdfJsTextExtractor(null);
const data = await readFile(PDF);
const doc = await getDocument({ data: new Uint8Array(data), useSystemFonts: true }).promise;
const pdfjsPage = await doc.getPage(PAGE);
const scale = 200 / 72;
const viewport = pdfjsPage.getViewport({ scale });
const canvas = createCanvas(Math.ceil(viewport.width), Math.ceil(viewport.height));
await pdfjsPage.render({ canvasContext: canvas.getContext("2d"), viewport }).promise;
const nodePng = canvas.toBuffer("image/png");
await doc.destroy();
await writeFile(`${OUT_DIR}/node-render-dpi200.png`, nodePng);
console.log(`Node render: ${canvas.width}x${canvas.height} (canvas native dimensions), PNG bytes=${nodePng.length}`);

// --- Render via Python's own pipeline code (PyMuPDF get_pixmap, dpi=200) ---
const pyRenderScript = `
import sys
sys.path.insert(0, r"D:\\Bills\\backend")
import fitz
doc = fitz.open(r"${PDF}")
pix = doc[${PAGE} - 1].get_pixmap(dpi=200)
with open(r"${OUT_DIR}\\python-render-dpi200.png", "wb") as f:
    f.write(pix.tobytes("png"))
print(f"{pix.width}x{pix.height} colorspace={pix.colorspace.name if pix.colorspace else None} alpha={pix.alpha} n={pix.n}")
`;
const pyRenderInfo = execFileSync("D:\\Bills\\backend\\.venv\\Scripts\\python.exe", ["-I", "-c", pyRenderScript], { encoding: "utf-8" }).trim();
console.log(`Python render (from PyMuPDF's own Pixmap object): ${pyRenderInfo}`);
const pyPngBuffer = await readFile(`${OUT_DIR}/python-render-dpi200.png`);
console.log(`Python PNG file size: ${pyPngBuffer.length} bytes`);

// --- PDF page's own native dimensions (points) ---
const pyPageDimsScript = `
import sys
sys.path.insert(0, r"D:\\Bills\\backend")
import fitz
doc = fitz.open(r"${PDF}")
r = doc[${PAGE} - 1].rect
print(f"{r.width}x{r.height} rotation={doc[${PAGE} - 1].rotation}")
`;
console.log(`PDF page ${PAGE} native size (points): ${execFileSync("D:\\Bills\\backend\\.venv\\Scripts\\python.exe", ["-I", "-c", pyPageDimsScript], { encoding: "utf-8" }).trim()}`);

// --- Cross-test: run Python's pytesseract against BOTH images ---
function pytesseractOn(pngPath) {
  const script = `
import sys, json
sys.path.insert(0, r"D:\\Bills\\backend")
from app.services.extraction.ocr_engine import TesseractOCREngine
with open(r"${pngPath}", "rb") as f:
    data = f.read()
engine = TesseractOCREngine()
result = engine.extract_text(data)
print(json.dumps({"text": result["text"], "confidence": result["confidence"], "textLength": len(result["text"].strip())}))
`;
  return JSON.parse(execFileSync("D:\\Bills\\backend\\.venv\\Scripts\\python.exe", ["-I", "-c", script], { encoding: "utf-8", maxBuffer: 20 * 1024 * 1024 }));
}

console.log("\n=== Cross-test matrix ===");
const pyOnPyRender = pytesseractOn(`${OUT_DIR}/python-render-dpi200.png`);
console.log(`pytesseract on PYTHON's own render: textLength=${pyOnPyRender.textLength}, confidence=${pyOnPyRender.confidence}`);
console.log(`  text: ${JSON.stringify(pyOnPyRender.text)}`);

const pyOnNodeRender = pytesseractOn(`${OUT_DIR}/node-render-dpi200.png`);
console.log(`pytesseract on NODE's render: textLength=${pyOnNodeRender.textLength}, confidence=${pyOnNodeRender.confidence}`);
console.log(`  text: ${JSON.stringify(pyOnNodeRender.text)}`);

// --- Cross-test: run tesseract.js against BOTH images ---
async function tesseractJsOn(pngBuffer, label) {
  const ocr = new TesseractJsOCREngine();
  const result = await ocr.extractText(pngBuffer);
  await ocr.terminate();
  console.log(`tesseract.js on ${label}: textLength=${result.text.trim().length}, confidence=${result.confidence}`);
  console.log(`  text: ${JSON.stringify(result.text)}`);
  return result;
}

const jsOnNodeRender = await tesseractJsOn(nodePng, "NODE's own render");
const jsOnPyRender = await tesseractJsOn(pyPngBuffer, "PYTHON's render");

await writeFile(`${OUT_DIR}/cross-test-results.json`, JSON.stringify({
  nodeRenderDims: `${canvas.width}x${canvas.height}`,
  pythonRenderInfo: pyRenderInfo,
  pyOnPyRender, pyOnNodeRender,
  jsOnNodeRender: { text: jsOnNodeRender.text, confidence: jsOnNodeRender.confidence },
  jsOnPyRender: { text: jsOnPyRender.text, confidence: jsOnPyRender.confidence },
}, null, 2));
console.log(`\nWrote ${OUT_DIR}/cross-test-results.json (and both rendered PNGs, for visual inspection)`);
