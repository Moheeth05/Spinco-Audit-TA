// DEV-ONLY diagnostics. Refuses to print anything for held-out / control / suspect receipts.
// Usage: node scripts/debug_dev.mjs <group: authentic|synthetic> [onlyFailures] [fieldFilter]
import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { CachedOCR } from "../src/ocr.mjs";
import { extractFuelReceipt } from "../src/fuelExtractor.mjs";
import { scoreReceipt } from "../src/scoring.mjs";

const FUEL = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const [group = "authentic", onlyFail = "1", fieldFilter = ""] = process.argv.slice(2);
const recs = JSON.parse(fs.readFileSync(path.join(FUEL, "corpus", "annotations.json"), "utf8")).records.filter((r) => r.split === "dev" && r.source_type === group);
const ocr = new CachedOCR(path.join(FUEL, "corpus", "ocr-cache"));
for (const rec of recs) {
  const dir = group === "authentic" ? "authentic" : "synthetic";
  const o = await ocr.read(path.join(FUEL, "corpus", dir, "images", `${rec.receipt_id}.png`), { mode: "retry" });
  const pred = extractFuelReceipt(o);
  const s = scoreReceipt(pred, rec);
  const bad = ["pump_name", "transaction_date", "total_amount"].filter((f) => s[f].labelled && !s[f].exact);
  if (onlyFail === "1" && !bad.length && !s.falseAccept) continue;
  if (fieldFilter && !bad.includes(fieldFilter)) continue;
  console.log(`\n##### ${rec.receipt_id}  [${rec.receipt_family}]  wrong: ${bad.join(",") || "-"}  review=${pred.overall_review_required}`);
  const FILTERS = { total_amount: /(amount|amt|total|sale|\brs|inr|\d{3,}\.\d|\d{3,}[,.]\s?\d)/i, transaction_date: /(date|\bdt\b|txst|txend|print|\d{1,2}\s?[\/\-.]\s?\d{1,2}\s?[\/\-.]\s?\d{2,4}|20\d\d)/i };
  const show = fieldFilter && FILTERS[fieldFilter] ? o.lines.map((l, i) => ({ l, i })).filter(({ l }) => FILTERS[fieldFilter].test(l.text)) : o.lines.slice(0, 16).map((l, i) => ({ l, i }));
  console.log(`  OCR lines (${fieldFilter || "first 16"}):`);
  show.forEach(({ l, i }) => console.log(`   ${String(i).padStart(2)} c=${(l.confidence ?? 0).toFixed(2)} ${l.text.replace(/\s+/g, " ")}`));
  for (const f of ["pump_name", "transaction_date", "total_amount"]) {
    const p = pred[f];
    console.log(`  ${f}: got=${JSON.stringify(p.normalized_value)} raw=${JSON.stringify(p.raw_value)} review=${p.review_required} | expected=${JSON.stringify(rec.fields[f].normalized_value)} | ${p.ambiguity || ""}`);
  }
}
await ocr.close();

