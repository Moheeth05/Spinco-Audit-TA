// The bounded OCR retry (mode "retry2") is tested with a STUB recognizer, so the policy (skip / order / stop / budget) is checked without running tesseract.
import test from "node:test";
import assert from "node:assert/strict";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import { CachedOCR, RETRY2 } from "../src/ocr.mjs";

const PNG = Buffer.from("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg==", "base64");
const GOOD = "PETROL DIESEL AMOUNT DATE 24/07/2026 TIME NOZZLE VOLUME RATE 2000.00 TOTAL"; // 'usable' by the policy's own test
const MID = "PETROL some words 24/07/2026"; // readable but not 'usable'
const text = (conf, t) => ({ text: t, confidence: conf, lines: [{ text: t, bbox: { x0: 20, y0: 0, x1: 80, y1: 10 }, confidence: conf, words: [] }] });

function harness(responses, { delayMs = 0 } = {}) {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), "fuel-retry-"));
  const img = path.join(dir, "x.png");
  fs.writeFileSync(img, PNG);
  const calls = [];
  const ocr = new CachedOCR(path.join(dir, "cache"));
  ocr._recognize = async (bytes, options) => {
    const key = options.rotateRadians === undefined ? (bytes.equals(PNG) ? "first" : "pre") : options.rotateRadians > 3 ? "r180" : options.rotateRadians > 0 ? "r+90" : "r-90";
    calls.push(key);
    if (delayMs) await new Promise((r) => setTimeout(r, delayMs));
    const r = responses[key] || responses.default || text(0.1, "");
    return { ...r, lines: r.lines.map((l) => ({ ...l, edge: null })) };
  };
  return { ocr, img, calls };
}

test("a confident first pass is never retried", async () => {
  const h = harness({ first: text(0.9, "short receipt text 24/07/2026 rs 500") });
  const r = await h.ocr.read(h.img, { mode: "retry2" });
  assert.deepEqual(h.calls, ["first"]);
  assert.equal(r.retry_stopped, "skipped_first_pass_confident");
});

test("a usable first pass is never retried", async () => {
  const h = harness({ first: text(0.55, GOOD) });
  const r = await h.ocr.read(h.img, { mode: "retry2" });
  assert.deepEqual(h.calls, ["first"]);
  assert.equal(r.retry_stopped, "first_pass_usable");
});

test("a weak non-empty first pass tries a ROTATION first and stops as soon as an attempt is confident", async () => {
  const h = harness({ first: text(0.3, "NIVOV LISIA MOA MNVHL 00 000 something garbled here"), "r+90": text(0.93, "INDIAN OIL CORPORATION LTD MAA TARA SERVICE STATION CASH MEMO") });
  const r = await h.ocr.read(h.img, { mode: "retry2" });
  assert.deepEqual(h.calls, ["first", "r+90"]);
  assert.equal(r.chosen_attempt, "rotate+90");
  assert.equal(r.retry_stopped, "confident_attempt");
});

test("an EMPTY first pass tries the production preprocessing first", async () => {
  const h = harness({ first: text(0.0, ""), pre: text(0.6, GOOD) });
  const r = await h.ocr.read(h.img, { mode: "retry2" });
  assert.equal(h.calls[1], "pre");
  assert.equal(r.chosen_attempt, "preprocessed");
});

test("never more than maxAttempts extra attempts", async () => {
  const h = harness({ first: text(0.2, "garbled garbled garbled garbled") , default: text(0.2, "still garbled garbled garbled") });
  const r = await h.ocr.read(h.img, { mode: "retry2" });
  assert.ok(h.calls.length - 1 <= RETRY2.maxAttempts, JSON.stringify(h.calls));
  assert.ok(["max_attempts", "plan_exhausted"].includes(r.retry_stopped));
});

test("the time budget stops further attempts (worst case is bounded)", async () => {
  const saved = RETRY2.budgetMs;
  RETRY2.budgetMs = 60; // first pass takes ~40 ms in the stub; another attempt is predicted to cost 1.5x that, so none fits
  try {
    const h = harness({ first: text(0.2, "garbled garbled garbled garbled"), default: text(0.2, "still garbled garbled") }, { delayMs: 40 });
    const r = await h.ocr.read(h.img, { mode: "retry2" });
    assert.equal(r.retry_stopped, "time_budget");
    assert.deepEqual(h.calls, ["first"]);
  } finally { RETRY2.budgetMs = saved; }
});

test("the retry never replaces a better first pass with a worse attempt", async () => {
  const h = harness({ first: text(0.45, "PETROL DIESEL AMOUNT DATE 24/07/2026 something"), default: text(0.1, "xx") });
  const r = await h.ocr.read(h.img, { mode: "retry2" });
  assert.equal(r.chosen_attempt, "first_pass");
});

test("single mode never retries", async () => {
  const h = harness({ first: text(0.1, "") });
  await h.ocr.read(h.img, { mode: "single" });
  assert.deepEqual(h.calls, ["first"]);
});
