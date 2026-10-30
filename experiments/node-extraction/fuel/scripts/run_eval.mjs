// Evaluate an extractor on the frozen corpus: three fields + overall review outcome, by group / family / difficulty.
// Usage: node scripts/run_eval.mjs --impl baseline|improved --label <name> [--only dev|heldout|all]
//
// Held-out discipline: per-receipt results for held-out groups go to reports/<label>.details.json only, and the console
// prints aggregates only. Dev per-receipt rows are printed so they can be used for diagnosis.
//
// Integrity preflight (src/evalPreflight.mjs): before any OCR and before any report is written the run REFUSES (exit 1, nothing written) if an argument is unknown, the intake
// register is invalid, nothing is selected, a selected image is missing / empty / (intake) differs from its registered SHA-256, a held-out intake receipt is selected without a
// valid held-out lock, or the register's held-out entries disagree with the lock. Scoring, thresholds and metrics are unchanged.
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { CachedOCR } from "../src/ocr.mjs";
import { aggregate, scoreReceipt } from "../src/scoring.mjs";
import { loadAnnotations } from "../src/labels.mjs";
import { lockStatus } from "../src/codeHash.mjs";
import { argProblems, evalPreflightProblems } from "../src/evalPreflight.mjs";

const HERE = path.dirname(fileURLToPath(import.meta.url));
const FUEL = path.resolve(HERE, "..");
const args = Object.fromEntries(process.argv.slice(2).reduce((a, x, i, all) => (x.startsWith("--") ? [...a, [x.slice(2), all[i + 1]]] : a), []));
const impl = args.impl || "baseline";
const label = args.label || impl;
const only = args.only || "all";
const ocrMode = args.ocr || (impl === "baseline" ? "single" : "retry2"); // baseline = the existing single pass; improved = single pass + the bounded retry2 policy

// PREFLIGHT (src/evalPreflight.mjs): everything that can be checked before OCR is checked first - the arguments, the intake register, a non-empty selection, every image
// the selected records will OCR (intake images against their registered SHA-256), and the held-out lock. Any problem: exit 1 with the reasons, NO OCR object is created
// (its constructor creates the cache folder), nothing is OCR'd and no report or detail file is written.
const MAX_REFUSAL_LINES = 25;
const refuse = (lines) => {
  const shown = lines.length > MAX_REFUSAL_LINES ? [...lines.slice(0, MAX_REFUSAL_LINES), `... and ${lines.length - MAX_REFUSAL_LINES} more`] : lines;
  console.error("REFUSED - evaluation not started (no OCR was run, no report or detail file was written):\n  " + shown.join("\n  "));
  process.exit(1);
};
const argIssues = argProblems({ impl, only, label, ocrMode, labels: args.labels });
if (argIssues.length) refuse(argIssues);

const extract = impl === "baseline" ? (await import("../src/baselineAdapter.mjs")).baselineExtract : (await import("../src/fuelExtractor.mjs")).extractFuelReceipt;
const { records: annotations, summary: labelSummary, eligibility } = loadAnnotations(FUEL, { labels: args.labels }); // --labels all | verified_only (default: from verification/label_policy.json)

const imagePath = (rec) => {
  if (rec.image_path) return path.join(FUEL, rec.image_path); // intake receipts carry their own path
  const dir = rec.source_type === "authentic" || rec.source_type === "suspected_template_generated" ? "authentic" : rec.split === "heldout2" ? "synthetic2" : rec.split === "heldout3" ? "synthetic3" : rec.split === "heldout4" ? "synthetic4" : "synthetic";
  return path.join(FUEL, "corpus", dir, "images", `${rec.receipt_id}.png`);
};

const GROUPS = {
  authentic_dev: (r) => r.source_type === "authentic" && r.split === "dev",
  authentic_heldout: (r) => r.source_type === "authentic" && r.split === "heldout",
  synthetic_dev: (r) => r.source_type === "synthetic" && r.split === "dev",
  synthetic_heldout: (r) => r.source_type === "synthetic" && r.split === "heldout",
  synthetic_heldout2: (r) => r.source_type === "synthetic" && r.split === "heldout2",
  synthetic_heldout3: (r) => r.source_type === "synthetic" && r.split === "heldout3",
  synthetic_heldout4: (r) => r.source_type === "synthetic" && r.split === "heldout4",
  suspected_template_generated: (r) => r.source_type === "suspected_template_generated",
  generic_controls: (r) => r.source_type === "generic_control",
  authentic_intake_dev: (r) => r.split === "intake_dev",
  authentic_intake_heldout: (r) => r.split === "intake_heldout",
};
const isHeldoutGroup = (g) => g.endsWith("heldout") || g.endsWith("heldout2") || g.endsWith("heldout3") || g.endsWith("heldout4") || g === "suspected_template_generated" || g === "generic_controls";
// Which records this run evaluates (the same filters as always), decided BEFORE any OCR so that preflight can check exactly their images.
const selected = [];
for (const rec of annotations) {
  const group = Object.entries(GROUPS).find(([, f]) => f(rec))?.[0];
  if (!group) continue;
  if (only === "dev" && isHeldoutGroup(group)) continue;
  if (only === "heldout" && !isHeldoutGroup(group)) continue;
  if (only === "heldout2" && group !== "synthetic_heldout2") continue;
  if (only === "heldout3" && group !== "synthetic_heldout3") continue;
  if (only === "heldout4" && group !== "synthetic_heldout4") continue;
  if (only === "dev" && group === "synthetic_heldout4") continue;
  if (only === "dev" && group === "synthetic_heldout3") continue;
  selected.push({ rec, group, file: imagePath(rec) });
}
const preflightProblems = evalPreflightProblems({ fuelDir: FUEL, summary: labelSummary, eligibility, selected });
if (preflightProblems.length) refuse(preflightProblems);

console.log(`labels: mode=${labelSummary.mode} human_verified=${labelSummary.human_verified} assistant-read provisional in use=${labelSummary.assistant_read_provisional_in_use} excluded=${labelSummary.assistant_read_excluded}`);
const ocr = new CachedOCR(path.join(FUEL, "corpus", "ocr-cache"));
const lock = lockStatus(FUEL);
if (lock.locked) console.log(`held-out intake lock: ${lock.receipts} receipt(s) locked ${lock.locked_at}; extractor ${lock.still_untouched ? "UNCHANGED since the lock (set is still untouched)" : "CHANGED since the lock (" + lock.changed_files.join(", ") + ") - the intake held-out set is now DEVELOPMENT data"}`);

const results = {};
const timing = {};
for (const { rec, group, file } of selected) {
  const o = await ocr.read(file, { mode: ocrMode });
  const t0 = performance.now();
  const pred = extract(o);
  const ms = performance.now() - t0;
  const scored = scoreReceipt(pred, rec);
  scored.family = rec.receipt_family;
  scored.difficulty = rec.difficulty;
  scored.group = group;
  scored.extractMs = ms;
  scored.pred = pred;
  (results[group] ||= []).push(scored);
  (timing[group] ||= []).push(ms);
}
await ocr.close();

const idx = JSON.parse(fs.readFileSync(path.join(FUEL, "corpus", "ocr-index.json"), "utf8")).images;
const out = { label, impl, ocr_mode: ocrMode, ocr_stats: ocr.stats, label_summary: labelSummary, heldout_intake_lock: lock, generated: new Date().toISOString(), hardware: { node: process.version, cpu: os.cpus()[0]?.model, cores: os.cpus().length, ramGB: Math.round(os.totalmem() / 2 ** 30), platform: os.platform() }, groups: {}, by_family: {}, by_difficulty: {} };
for (const [g, rows] of Object.entries(results)) {
  out.groups[g] = aggregate(rows);
  const ocrMs = rows.map((r) => idx[r.id]?.ms).filter((x) => typeof x === "number");
  out.groups[g].timing = { extract_ms_mean: timing[g].reduce((a, b) => a + b, 0) / timing[g].length, ocr_ms_mean_first_run_only: ocrMs.length ? ocrMs.reduce((a, b) => a + b, 0) / ocrMs.length : null, ocr_note: "OCR cost is cached; the mean covers only receipts OCR'd in this corpus build" };
  for (const r of rows) {
    ((out.by_family[`${g}:${r.family}`] ||= []).push(r));
    for (const d of r.difficulty || []) (out.by_difficulty[`${g}:${d}`] ||= []).push(r);
  }
}
const compact = (map) => Object.fromEntries(Object.entries(map).map(([k, rows]) => [k, { receipts: rows.length, ...Object.fromEntries(["pump_name", "transaction_date", "total_amount"].map((f) => [f, `${rows.filter((r) => r[f].exact).length}/${rows.filter((r) => r[f].labelled).length} exact`])), review_required: `${rows.filter((r) => r.overallReviewRequired).length}/${rows.length}`, false_accept: `${rows.filter((r) => r.falseAccept).length}/${rows.length}` }]));
out.by_family = compact(out.by_family);
out.by_difficulty = compact(out.by_difficulty);
fs.mkdirSync(path.join(FUEL, "reports"), { recursive: true });
fs.writeFileSync(path.join(FUEL, "reports", `${label}.json`), JSON.stringify(out, null, 1));
fs.writeFileSync(path.join(FUEL, "reports", `${label}.details.json`), JSON.stringify(results, null, 1));

const pct = (x) => (x.d ? `${x.n}/${x.d} (${(100 * x.rate).toFixed(0)}%)` : `0/0`);
console.log(`\n=== ${label} (${impl}) ===`);
for (const [g, a] of Object.entries(out.groups)) {
  console.log(`\n[${g}] receipts=${a.receipts}`);
  for (const f of ["pump_name", "transaction_date", "total_amount"]) {
    const x = a[f];
    console.log(`  ${f.padEnd(17)} exact ${pct(x.exact_rate)}  coverage ${pct(x.coverage)}  falseConfident ${pct(x.false_confident_per_receipt)}`);
  }
  const d = a.transaction_date;
  console.log(`  date: resolvable-alone exact ${d.resolvable_on_receipt_alone.exact}/${d.resolvable_on_receipt_alone.receipts}; ambiguous-alone returned ${d.ambiguous_on_receipt_alone.returned_a_date}/${d.ambiguous_on_receipt_alone.receipts}, held for review ${d.ambiguous_on_receipt_alone.held_for_review}`);
  console.log(`  overall: review ${pct(a.overall.review_required_rate)}  justified ${pct(a.overall.review_justified)}  trueAccept ${pct(a.overall.true_accept)}  falseAccept ${pct(a.overall.false_accept_rate)}`);
}
if (args.verbose) {
  for (const [g, rows] of Object.entries(results)) {
    if (isHeldoutGroup(g)) continue;
    console.log(`\n--- per-receipt (dev only): ${g}`);
    for (const r of rows) console.log(`${r.id.padEnd(40)} pump:${r.pump_name.exact ? "OK " : "-- "}${String(r.pump_name.got).slice(0, 28).padEnd(28)} date:${r.transaction_date.exact ? "OK " : "-- "}${String(r.transaction_date.got).padEnd(10)} amt:${r.total_amount.exact ? "OK " : "-- "}${String(r.total_amount.got).padEnd(9)} review:${r.overallReviewRequired ? "Y" : "n"}`);
  }
}
