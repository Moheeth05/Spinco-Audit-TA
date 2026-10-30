// Step 2: controlled matrix - render the SAME page via PYTHON's own
// pipeline code at several DPI values (bracketing the embedded JPEG's
// native ~96 DPI, confirmed via PyMuPDF's own extract_image metadata:
// 863x1435px placed into a 489x814pt area => ~96 effective DPI) and run
// pytesseract on each, changing only DPI - everything else (renderer,
// OCR engine, image format) held constant.
import { execFileSync } from "node:child_process";
import { writeFile } from "node:fs/promises";

const PDF = "fixtures/reference-pdfs/vivek-bhai-report-ER41220.pdf";
const PAGE = 6;
const OUT_DIR = "reports/ocr-diagnostic-page6";
const DPI_VALUES = [96, 150, 200, 300, 400];

const results = [];
for (const dpi of DPI_VALUES) {
  const script = `
import sys, json
sys.path.insert(0, r"D:\\Bills\\backend")
import fitz
from app.services.extraction.ocr_engine import TesseractOCREngine

doc = fitz.open(r"${PDF}")
pix = doc[${PAGE} - 1].get_pixmap(dpi=${dpi})
png_bytes = pix.tobytes("png")
with open(r"${OUT_DIR}\\python-render-dpi${dpi}.png", "wb") as f:
    f.write(png_bytes)

engine = TesseractOCREngine()
result = engine.extract_text(png_bytes)
print(json.dumps({
    "dpi": ${dpi},
    "pixelDims": f"{pix.width}x{pix.height}",
    "textLength": len(result["text"].strip()),
    "confidence": result["confidence"],
    "text": result["text"][:300],
}))
`;
  const out = JSON.parse(execFileSync("D:\\Bills\\backend\\.venv\\Scripts\\python.exe", ["-I", "-c", script], { encoding: "utf-8", maxBuffer: 20 * 1024 * 1024 }));
  results.push(out);
  console.log(`dpi=${dpi} pixelDims=${out.pixelDims}: textLength=${out.textLength} confidence=${out.confidence.toFixed(3)}`);
  console.log(`  text preview: ${JSON.stringify(out.text.slice(0, 120))}`);
}

await writeFile(`${OUT_DIR}/dpi-matrix-results.json`, JSON.stringify(results, null, 2));
console.log(`\nWrote ${OUT_DIR}/dpi-matrix-results.json`);
