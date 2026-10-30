// Where does the retry's time go, and does each retry earn its cost?  (cold cache, every image, per-attempt timings)
//   * lists the slowest images with their attempt sequence and per-attempt milliseconds;
//   * for every retried image compares the three-field OUTCOME of the first pass with the outcome after the retry, against the labels:
//       improved = more exact fields, same = no change in exact fields, worsened = fewer exact fields, plus any change in false-confident / usable.
// Usage: node scripts/investigate_retry_latency.mjs [--mode retry|retry2] [--out reports/<file>.json]
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { CachedOCR } from "../src/ocr.mjs";
import { extractFuelReceipt } from "../src/fuelExtractor.mjs";
import { scoreReceipt } from "../src/scoring.mjs";
import { loadAnnotations } from "../src/labels.mjs";

const FUEL = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const args = Object.fromEntries(process.argv.slice(2).reduce((a, x, i, all) => (x.startsWith("--") ? [...a, [x.slice(2), all[i + 1]]] : a), []));
const mode = args.mode || "retry";
const { records } = loadAnnotations(FUEL, { labels: "all" });
const dirOf = (r) => (r.image_path ? null : r.source_type === "authentic" || r.source_type === "suspected_template_generated" ? "authentic" : r.split === "heldout2" ? "synthetic2" : r.split === "heldout3" ? "synthetic3" : r.split === "heldout4" ? "synthetic4" : "synthetic");
const imgPath = (r) => (r.image_path ? path.join(FUEL, r.image_path) : path.join(FUEL, "corpus", dirOf(r), "images", `${r.receipt_id}.png`));

const cacheSingle = new CachedOCR(fs.mkdtempSync(path.join(os.tmpdir(), "fuel-lat-s-")));
const cacheRetry = new CachedOCR(fs.mkdtempSync(path.join(os.tmpdir(), "fuel-lat-r-")));
const rows = [];
for (const r of records) {
  if (r.annotation_status === "pending_human_label") continue;
  const p = imgPath(r);
  const a = await cacheSingle.read(p, { mode: "single" });
  const t0 = Date.now();
  const b = await cacheRetry.read(p, { mode });
  const retryMs = Date.now() - t0;
  const attempts = b.attempts || [];
  const sa = scoreReceipt(extractFuelReceipt(a), r), sb = scoreReceipt(extractFuelReceipt(b), r);
  const exact = (s) => ["pump_name", "transaction_date", "total_amount"].filter((f) => s[f].exact).length;
  const fc = (s) => ["pump_name", "transaction_date", "total_amount"].filter((f) => s[f].falseConfident).length;
  const retried = attempts.length > 1;
  rows.push({ id: r.receipt_id, kind: r.source_type, family: r.receipt_family, retried, attempts: attempts.map((x) => `${x.name}:${x.ms ?? "?"}ms`), total_ms: retryMs,
    exact_first: exact(sa), exact_final: exact(sb), falseconf_first: fc(sa), falseconf_final: fc(sb), falseaccept_first: sa.falseAccept, falseaccept_final: sb.falseAccept, trueaccept_first: sa.trueAccept, trueaccept_final: sb.trueAccept,
    chosen: b.chosen_attempt, first_conf: a.confidence, final_conf: b.confidence });
}
await cacheSingle.close(); await cacheRetry.close();
const ret = rows.filter((x) => x.retried);
const cls = (x) => (x.exact_final > x.exact_first ? "improved" : x.exact_final < x.exact_first ? "worsened" : "same");
const summary = {
  mode, images: rows.length, retried: ret.length,
  retried_by_kind: Object.fromEntries([...new Set(ret.map((x) => x.kind))].map((k) => [k, `${ret.filter((x) => x.kind === k).length}/${rows.filter((x) => x.kind === k).length}`])),
  outcome_of_retried: { improved: ret.filter((x) => cls(x) === "improved").length, same: ret.filter((x) => cls(x) === "same").length, worsened: ret.filter((x) => cls(x) === "worsened").length },
  retry_changed_false_confident: ret.filter((x) => x.falseconf_final !== x.falseconf_first).map((x) => `${x.id}: ${x.falseconf_first}->${x.falseconf_final}`),
  retry_changed_false_accept: ret.filter((x) => x.falseaccept_final !== x.falseaccept_first).map((x) => `${x.id}: ${x.falseaccept_first}->${x.falseaccept_final}`),
  retry_changed_true_accept: ret.filter((x) => x.trueaccept_final !== x.trueaccept_first).map((x) => `${x.id}: ${x.trueaccept_first}->${x.trueaccept_final}`),
  total_s_all_images: rows.reduce((a, b) => a + b.total_ms, 0) / 1000,
  slowest: [...rows].sort((a, b) => b.total_ms - a.total_ms).slice(0, 8).map((x) => ({ id: x.id, total_ms: x.total_ms, attempts: x.attempts, chosen: x.chosen, first_conf: x.first_conf, final_conf: x.final_conf })),
  retried_that_did_not_help: ret.filter((x) => cls(x) === "same").length,
  wasted_ms_on_retries_that_changed_nothing: ret.filter((x) => cls(x) === "same").reduce((a, b) => a + b.total_ms, 0),
  by_improvement: ret.filter((x) => cls(x) !== "same").map((x) => `${x.id}: ${cls(x)} (${x.exact_first}->${x.exact_final} exact fields, via ${x.chosen}, ${x.total_ms}ms)`),
};
const outFile = path.resolve(args.out || path.join(FUEL, "reports", `latency-investigation-${mode}.json`));
fs.writeFileSync(outFile, JSON.stringify({ generated: new Date().toISOString(), summary, rows }, null, 1));
console.log(JSON.stringify(summary, null, 1));
