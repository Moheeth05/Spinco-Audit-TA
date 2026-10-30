// Local OCR (tesseract.js, WASM) with a content-hash cache and an OPTIONAL bounded retry.
//
// Returns the plain text AND the line/word geometry the position-aware extractor needs:
//   { text, confidence (0-1), lines: [{ text, bbox, confidence, words:[{text,bbox,confidence}] }], source, attempts, chosen_attempt, retry_stopped }
//
// mode "single" = exactly one tesseract.js pass (what the existing experiment does). Baseline and attribution.
// mode "retry"  = v1: first pass; if it looks unusable try up to six more attempts (preprocess, +90, -90, 180, preprocess+90, preprocess-90). Kept for comparison.
// mode "retry2" = v2 (bounded): see RETRY2 below. Chosen from the latency investigation (reports/latency-investigation-retry.json): 47 of 57 v1 retries changed
//                 nothing and cost ~176 s of ~290 s; the worst image took 38.7 s; the 10 retries that helped were one 90-degree rotation or the production
//                 grey-contrast-threshold preprocessing. v2 therefore (1) skips the retry when the first pass is already confident, (2) orders the attempts by what
//                 the first pass looks like, (3) stops at the first usable result, after 4 attempts, or when the time budget would be exceeded.
//
// Language data is read from the LOCAL copy next to this package (never the CDN). Results are cached on disk keyed by image SHA-256 + settings + mode.

import { createWorker } from "tesseract.js";
import crypto from "node:crypto";
import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { decodePng, encodeGreyPng, preprocessForDifficultOcr } from "./png.mjs";

const LOCAL_LANG_DIR = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");

const SETTINGS = { lang: "eng", rotateAuto: true, engine: "tesseract.js@7", retryRecipe: "v1: preprocess(grey,contrast1.6,thr140)+rot(90,-90,180)+pre-rot; v2: bounded", lineEdgeFlags: "v1 (image-edge touching lines flagged, 6px)", retry2: "stop on confident attempt; skip when conf>=0.7 and >=30 chars" };
// v2 policy. The numbers come from the development evidence above and are heuristics, not calibrated values.
export const RETRY2 = { stopOnConfidentAttempt: { confidence: 0.8, chars: 40 }, skipIfConfidenceAtLeast: 0.7, skipOnlyIfCharsAtLeast: 30, emptyFirstPassChars: 20, maxAttempts: 4, budgetMs: 12000, attemptCostFactor: 1.5 };

const KEYWORDS = ["PETROL", "DIESEL", "AMOUNT", "DATE", "SALE", "NOZZLE", "NOZL", "VOLUME", "RATE", "TOTAL", "PRODUCT", "DENSITY", "BILL", "INV", "TIME", "RECEIPT", "FUEL", "BHARAT", "INDIAN", "HP", "BPCL", "SHELL", "NAYARA", "PETROLEUM", "THANK", "TXN", "PRESET"];
export function ocrScore(text, confidence) {
  const up = (text || "").toUpperCase();
  const kw = KEYWORDS.filter((k) => new RegExp(`\\b${k}`).test(up)).length;
  const dates = (up.match(/\b\d{1,2}\s?[\/\-.]\s?\d{1,2}\s?[\/\-.]\s?\d{2,4}\b/g) || []).length;
  const money = (up.match(/\b\d{2,}\.\d{2}\b/g) || []).length;
  return { score: kw * 3 + Math.min(dates, 2) * 2 + Math.min(money, 2) * 2 + (confidence || 0) * 5, keywords: kw, dates, money };
}
const looksUsable = (text, confidence) => {
  const s = ocrScore(text, confidence);
  return s.keywords >= 5 && (s.dates > 0 || s.money > 0) && (confidence || 0) >= 0.45;
};
const charCount = (t) => (t || "").replace(/\s/g, "").length;
const EDGE_PX = 6;
// PNG pixel size from the IHDR chunk (null for other formats: edge flags are then simply absent, never guessed).
function pngSize(bytes) {
  if (bytes.length > 24 && bytes[0] === 0x89 && bytes.toString("ascii", 1, 4) === "PNG") return { width: bytes.readUInt32BE(16), height: bytes.readUInt32BE(20) };
  return null;
}

export class CachedOCR {
  constructor(cacheDir) {
    this.cacheDir = cacheDir;
    this.worker = null;
    fs.mkdirSync(cacheDir, { recursive: true });
    this.stats = { hits: 0, misses: 0, ocrMs: 0, retriedImages: 0 };
  }

  async _worker() {
    if (!this.worker) {
      // langPath/cachePath point at the local copy so tesseract.js never contacts its default CDN (verified by scripts/verify_offline.mjs).
      this.worker = await createWorker(SETTINGS.lang, 1, { langPath: LOCAL_LANG_DIR, cachePath: LOCAL_LANG_DIR, gzip: false });
    }
    return this.worker;
  }

  async _recognize(bytes, options) {
    const worker = await this._worker();
    const { data } = await worker.recognize(bytes, options, { text: true, blocks: true });
    // extents of the image the coordinates refer to (swapped when the engine or the caller rotated it by +-90 degrees)
    const size = pngSize(bytes);
    const rot = Math.abs(typeof options.rotateRadians === "number" ? options.rotateRadians : data.rotateRadians || 0);
    const quarter = Math.abs(rot - Math.PI / 2) < 0.2 || Math.abs(rot - (3 * Math.PI) / 2) < 0.2;
    const W = size ? (quarter ? size.height : size.width) : null;
    const lines = [];
    for (const block of data.blocks || []) {
      for (const para of block.paragraphs || []) {
        for (const line of para.lines || []) {
          const text = (line.text || "").replace(/\s+$/g, "");
          if (!text.trim()) continue;
          const edge = W ? { left: line.bbox.x0 <= EDGE_PX, right: line.bbox.x1 >= W - EDGE_PX } : null;
          lines.push({
            text,
            bbox: line.bbox,
            edge,
            confidence: typeof line.confidence === "number" ? line.confidence / 100 : null,
            words: (line.words || []).map((w) => ({ text: w.text, bbox: w.bbox, confidence: typeof w.confidence === "number" ? w.confidence / 100 : null })),
          });
        }
      }
    }
    lines.sort((a, b) => a.bbox.y0 - b.bbox.y0 || a.bbox.x0 - b.bbox.x0);
    return { text: data.text || "", confidence: typeof data.confidence === "number" ? data.confidence / 100 : 0, lines };
  }

  async read(imagePath, { mode = "single" } = {}) {
    const bytes = fs.readFileSync(imagePath);
    const sha = crypto.createHash("sha256").update(bytes).digest("hex");
    const key = crypto.createHash("sha256").update(sha).update(JSON.stringify(SETTINGS)).update(mode).digest("hex").slice(0, 32);
    const file = path.join(this.cacheDir, `${key}.json`);
    if (fs.existsSync(file)) {
      this.stats.hits++;
      return { ...JSON.parse(fs.readFileSync(file, "utf8")), cache: "hit" };
    }
    const t0 = Date.now();
    const first = await this._recognize(bytes, { rotateAuto: SETTINGS.rotateAuto });
    const firstMs = Date.now() - t0;
    const s0 = ocrScore(first.text, first.confidence);
    let best = { ...first, attempt: "first_pass", ...s0 };
    const attempts = [{ name: "first_pass", ms: firstMs, score: round(s0.score), keywords: s0.keywords, confidence: round(first.confidence) }];
    let stopped = null;

    const preprocessed = () => { try { return encodeGreyPng(preprocessForDifficultOcr(decodePng(bytes))); } catch (e) { attempts.push({ name: "preprocess", skipped: String(e.message) }); return null; } };
    const consider = async (name, img, opts) => {
      const ta = Date.now();
      const r = await this._recognize(img, opts);
      const s = ocrScore(r.text, r.confidence);
      attempts.push({ name, ms: Date.now() - ta, score: round(s.score), keywords: s.keywords, confidence: round(r.confidence) });
      if (s.score > best.score + 0.5) best = { ...r, attempt: name, ...s };
    };

    if (mode === "retry" && !looksUsable(first.text, first.confidence)) {
      this.stats.retriedImages++;
      const pre = preprocessed();
      const plan = [];
      if (pre) plan.push(["preprocessed", pre, { rotateAuto: true }]);
      plan.push(["rotate+90", bytes, { rotateRadians: Math.PI / 2 }], ["rotate-90", bytes, { rotateRadians: -Math.PI / 2 }], ["rotate180", bytes, { rotateRadians: Math.PI }]);
      if (pre) plan.push(["preprocessed+90", pre, { rotateRadians: Math.PI / 2 }], ["preprocessed-90", pre, { rotateRadians: -Math.PI / 2 }]);
      for (const [name, img, opts] of plan) {
        await consider(name, img, opts);
        if (looksUsable(best.text, best.confidence)) break;
      }
    } else if (mode === "retry2") {
      const P = RETRY2;
      if (first.confidence >= P.skipIfConfidenceAtLeast && charCount(first.text) >= P.skipOnlyIfCharsAtLeast) stopped = "skipped_first_pass_confident";
      else if (looksUsable(first.text, first.confidence)) stopped = "first_pass_usable";
      else {
        this.stats.retriedImages++;
        const empty = charCount(first.text) < P.emptyFirstPassChars;
        const pre = empty ? preprocessed() : null; // preprocessing is tried FIRST only when the first pass returned (almost) nothing; otherwise rotations come first
        const plan = empty
          ? [...(pre ? [["preprocessed", pre, { rotateAuto: true }]] : []), ["rotate+90", bytes, { rotateRadians: Math.PI / 2 }], ["rotate-90", bytes, { rotateRadians: -Math.PI / 2 }], ["rotate180", bytes, { rotateRadians: Math.PI }]]
          : [["rotate+90", bytes, { rotateRadians: Math.PI / 2 }], ["rotate-90", bytes, { rotateRadians: -Math.PI / 2 }], ["preprocessed", "LAZY", { rotateAuto: true }], ["rotate180", bytes, { rotateRadians: Math.PI }]];
        let n = 0;
        stopped = "plan_exhausted";
        for (const [name, img0, opts] of plan) {
          if (n >= P.maxAttempts) { stopped = "max_attempts"; break; }
          const elapsed = Date.now() - t0;
          if (elapsed + P.attemptCostFactor * firstMs > P.budgetMs) { stopped = "time_budget"; break; }
          let img = img0;
          if (img === "LAZY") { img = preprocessed(); if (!img) continue; }
          await consider(name, img, opts);
          n++;
          if (looksUsable(best.text, best.confidence)) { stopped = "usable"; break; }
          if (best.confidence >= P.stopOnConfidentAttempt.confidence && charCount(best.text) >= P.stopOnConfidentAttempt.chars) { stopped = "confident_attempt"; break; }
        }
      }
    }
    const result = {
      text: best.text,
      confidence: best.confidence,
      lines: best.lines,
      source: `${SETTINGS.engine}${SETTINGS.rotateAuto ? "+rotateAuto" : ""}${mode === "single" ? "" : "+" + mode}`,
      chosen_attempt: best.attempt,
      retry_stopped: stopped,
      attempts,
      imageSha256: sha,
    };
    fs.writeFileSync(file, JSON.stringify(result));
    this.stats.misses++;
    this.stats.ocrMs += Date.now() - t0;
    return { ...result, cache: "miss" };
  }

  async close() {
    if (this.worker) await this.worker.terminate();
    this.worker = null;
  }
}

const round = (x) => Math.round(x * 100) / 100;
