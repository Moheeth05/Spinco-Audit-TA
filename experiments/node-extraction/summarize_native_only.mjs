import fs from "node:fs";
const rows = JSON.parse(fs.readFileSync("./corpus-comparison-results.json", "utf-8"));
const native = rows.filter((r) => r.hasNativeText);
console.log(`Native-text pages: ${native.length}`);
let allAgree = 0;
for (const r of native) {
  const ok = r.amountVerdict !== "DISAGREE" && r.dateVerdict !== "DISAGREE" && r.categoryVerdict !== "DISAGREE";
  console.log(`${ok ? "OK  " : "FAIL"} ${r.file} p${r.page}: amount=${r.amountVerdict} date=${r.dateVerdict} category=${r.categoryVerdict}`);
  if (ok) allAgree++;
}
console.log(`\nFully agree on all 3 fields: ${allAgree}/${native.length}`);
