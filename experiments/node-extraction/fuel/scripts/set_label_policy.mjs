// Records the company's decision about assistant-read labels. Usage:
//   node scripts/set_label_policy.mjs --assistant-labels include|exclude [--ai-review yes|no|unknown] [--confirmed-by "Name, role"]
import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

const FUEL = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const f = path.join(FUEL, "verification", "label_policy.json");
const a = Object.fromEntries(process.argv.slice(2).reduce((acc, x, i, all) => (x.startsWith("--") ? [...acc, [x.slice(2), all[i + 1]]] : acc), []));
const p = JSON.parse(fs.readFileSync(f, "utf8"));
if (a["assistant-labels"]) {
  if (!["include", "exclude"].includes(a["assistant-labels"])) throw new Error("--assistant-labels must be include or exclude");
  p.assistant_read_labels = a["assistant-labels"] === "exclude" ? "excluded" : "provisional_included";
}
if (a["ai-review"]) {
  if (!["yes", "no", "unknown"].includes(a["ai-review"])) throw new Error("--ai-review must be yes, no or unknown");
  p.ai_review_of_company_receipts_permitted = a["ai-review"];
  if (a["ai-review"] !== "unknown") {
    if (!a["confirmed-by"]) throw new Error("--confirmed-by is required when recording a yes/no decision");
    p.decision_confirmed_by = a["confirmed-by"];
    p.decision_recorded_at = new Date().toISOString();
  }
}
fs.writeFileSync(f, JSON.stringify(p, null, 2));
console.log(JSON.stringify({ assistant_read_labels: p.assistant_read_labels, ai_review_of_company_receipts_permitted: p.ai_review_of_company_receipts_permitted, decision_confirmed_by: p.decision_confirmed_by ?? null }));
