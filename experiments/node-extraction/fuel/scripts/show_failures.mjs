// Print failures from a details file (used AFTER a first look, when those receipts become development material).
// Usage: node scripts/show_failures.mjs <details.json> <group> [onlyFalseAccept 1|0]
import fs from "node:fs";
const [file, group, onlyFA = "0"] = process.argv.slice(2);
const d = JSON.parse(fs.readFileSync(file, "utf8"))[group];
for (const r of d) {
  const bad = ["pump_name", "transaction_date", "total_amount"].filter((f) => r[f].labelled && !r[f].exact);
  const fc = ["pump_name", "transaction_date", "total_amount"].filter((f) => r[f].falseConfident);
  if (onlyFA === "1" ? !r.falseAccept : !bad.length) continue;
  console.log(`\n${r.id} [${r.family}] diff=${(r.difficulty || []).join(",")} wrong=${bad.join(",")} falseConfident=${fc.join(",") || "-"} review=${r.overallReviewRequired}`);
  for (const f of bad) console.log(`   ${f}: got=${JSON.stringify(r[f].got)} expected=${JSON.stringify(r[f].expected)} review=${r.pred[f].review_required} raw=${JSON.stringify(r.pred[f].raw_value)} | ${r.pred[f].ambiguity || ""}`);
}
