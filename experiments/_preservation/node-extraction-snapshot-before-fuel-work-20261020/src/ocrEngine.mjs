// Tesseract.js-backed OCR - the WASM-compiled Tesseract engine, the same
// underlying OCR engine the Python side wraps via pytesseract (a different
// binding to the same C++ engine, not a different algorithm). Exact
// text-for-text parity with the Python side is not expected or claimed -
// see README.md's honest assessment of this stage.

import { createWorker } from "tesseract.js";

export class TesseractJsOCREngine {
  constructor() {
    this._worker = null;
  }

  async _getWorker() {
    if (!this._worker) {
      this._worker = await createWorker("eng");
    }
    return this._worker;
  }

  async extractText(imageBytes) {
    const worker = await this._getWorker();
    const { data } = await worker.recognize(Buffer.from(imageBytes));
    // tesseract.js's `data.text` is already newline-structured per its own
    // line-layout analysis (mirrors pytesseract's block/par/line grouping
    // closely enough for this comparison - both ultimately come from the
    // same underlying Tesseract layout analysis).
    const confidence = typeof data.confidence === "number" ? data.confidence / 100 : 0.0;
    return { text: data.text || "", confidence };
  }

  async terminate() {
    if (this._worker) {
      await this._worker.terminate();
      this._worker = null;
    }
  }
}
