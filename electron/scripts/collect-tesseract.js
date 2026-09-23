"use strict";

/**
 * Phase 11 - vendors a minimal Tesseract OCR runtime into resources/tesseract/
 * so the packaged desktop app never depends on the end user (or the
 * developer's machine) having Tesseract installed system-wide.
 *
 * Source: a real local Tesseract-OCR install, located via (in order):
 *   1. the TESSERACT_SOURCE_DIR env var
 *   2. C:\Program Files\Tesseract-OCR (the standard installer's default)
 *
 * Only what tesseract.exe actually needs at runtime is copied - not the ~100
 * language packs, training utilities, or docs a full install carries:
 *   - tesseract.exe and every *.dll next to it (safer than hand-picking a
 *     "minimal" DLL subset, which risks a missing transitive dependency
 *     that only breaks in production)
 *   - tessdata/eng.traineddata + tessdata/osd.traineddata (English text +
 *     orientation/script detection - this app processes English business
 *     documents, see app/services/extraction/ocr_engine.py)
 *
 * Tesseract is Apache-2.0 licensed, so bundling its binaries with this
 * application is permitted; the license file is copied alongside it.
 *
 * Re-run any time with: npm run collect:tesseract
 */

const fs = require("node:fs");
const path = require("node:path");

const SOURCE_DIR = process.env.TESSERACT_SOURCE_DIR || "C:\\Program Files\\Tesseract-OCR";
const DEST_DIR = path.join(__dirname, "..", "..", "resources", "tesseract");
const REQUIRED_TRAINEDDATA = ["eng.traineddata", "osd.traineddata"];

function main() {
  if (!fs.existsSync(path.join(SOURCE_DIR, "tesseract.exe"))) {
    console.warn(
      `[collect-tesseract] No Tesseract install found at "${SOURCE_DIR}" ` +
        `(tesseract.exe missing). Skipping - OCR fallback will not work in ` +
        `the packaged app unless resources/tesseract is populated manually ` +
        `or TESSERACT_SOURCE_DIR is set to a valid install before running ` +
        `this script again.`
    );
    return;
  }

  fs.rmSync(DEST_DIR, { recursive: true, force: true });
  fs.mkdirSync(path.join(DEST_DIR, "tessdata"), { recursive: true });

  fs.copyFileSync(path.join(SOURCE_DIR, "tesseract.exe"), path.join(DEST_DIR, "tesseract.exe"));

  let dllCount = 0;
  for (const entry of fs.readdirSync(SOURCE_DIR)) {
    if (entry.toLowerCase().endsWith(".dll")) {
      fs.copyFileSync(path.join(SOURCE_DIR, entry), path.join(DEST_DIR, entry));
      dllCount++;
    }
  }

  let dataCount = 0;
  for (const name of REQUIRED_TRAINEDDATA) {
    const src = path.join(SOURCE_DIR, "tessdata", name);
    if (fs.existsSync(src)) {
      fs.copyFileSync(src, path.join(DEST_DIR, "tessdata", name));
      dataCount++;
    } else {
      console.warn(`[collect-tesseract] Missing expected tessdata file: ${name}`);
    }
  }

  const licenseSrc = path.join(SOURCE_DIR, "doc", "LICENSE") ;
  if (fs.existsSync(licenseSrc)) {
    fs.copyFileSync(licenseSrc, path.join(DEST_DIR, "LICENSE"));
  }

  console.log(
    `[collect-tesseract] Vendored tesseract.exe + ${dllCount} DLLs + ${dataCount}/${REQUIRED_TRAINEDDATA.length} ` +
      `traineddata files from "${SOURCE_DIR}" into ${DEST_DIR}`
  );
}

main();
