// Rigorous parity harness: for each test case, runs the JS port AND spawns
// the real Python module (via the project's own venv) to get its actual
// output, then diffs them - not eyeballing two implementations side by
// side, genuinely comparing against the real production code's real
// behavior for the same input.
import { execFileSync } from "node:child_process";
import { findAllDates } from "./src/dates.mjs";
import { parseAmount, amountsMatch } from "./src/money.mjs";
import { normalizeCategory, isPolicyMasterCategory, expectedBillLabel } from "./src/categoryMap.mjs";

const PYTHON = "D:\\Bills\\backend\\.venv\\Scripts\\python.exe";

// Python literal syntax differs from JSON for null/true/false - this
// converts a JS value into the Python source text to embed in a -c script.
function toPy(value) {
  if (value === null || value === undefined) return "None";
  if (value === true) return "True";
  if (value === false) return "False";
  return JSON.stringify(value);
}

function pyDates(text) {
  const script = `
import sys, json
sys.path.insert(0, r"D:\\Bills\\backend")
from app.services.extraction.dates import find_all_dates
dates = find_all_dates(${toPy(text)})
print(json.dumps([d.isoformat() for d in dates]))
`;
  return JSON.parse(execFileSync(PYTHON, ["-I", "-c", script], { encoding: "utf-8" }));
}

function pyAmount(text) {
  const script = `
import sys, json
sys.path.insert(0, r"D:\\Bills\\backend")
from app.services.extraction.money import parse_amount
print(json.dumps(parse_amount(${toPy(text)})))
`;
  return JSON.parse(execFileSync(PYTHON, ["-I", "-c", script], { encoding: "utf-8" }));
}

function pyAmountsMatch(claimed, bill) {
  const script = `
import sys, json
sys.path.insert(0, r"D:\\Bills\\backend")
from app.services.extraction.money import amounts_match
print(json.dumps(amounts_match(${toPy(claimed)}, ${toPy(bill)})))
`;
  return JSON.parse(execFileSync(PYTHON, ["-I", "-c", script], { encoding: "utf-8" }));
}

function pyNormalizeCategory(text) {
  const script = `
import sys, json
sys.path.insert(0, r"D:\\Bills\\backend")
from app.services.category_map import normalize_category
print(json.dumps(normalize_category(${toPy(text)})))
`;
  return JSON.parse(execFileSync(PYTHON, ["-I", "-c", script], { encoding: "utf-8" }));
}

function pyIsPolicyMasterCategory(text) {
  const script = `
import sys, json
sys.path.insert(0, r"D:\\Bills\\backend")
from app.services.category_map import is_policy_master_category
print(json.dumps(is_policy_master_category(${toPy(text)})))
`;
  return JSON.parse(execFileSync(PYTHON, ["-I", "-c", script], { encoding: "utf-8" }));
}

function pyExpectedBillLabel(text) {
  const script = `
import sys, json
sys.path.insert(0, r"D:\\Bills\\backend")
from app.services.category_map import expected_bill_label
print(json.dumps(expected_bill_label(${toPy(text)})))
`;
  return JSON.parse(execFileSync(PYTHON, ["-I", "-c", script], { encoding: "utf-8" }));
}

let pass = 0, fail = 0;
function check(label, actual, expected) {
  const a = JSON.stringify(actual);
  const e = JSON.stringify(expected);
  if (a === e) {
    pass++;
  } else {
    fail++;
    console.log(`[FAIL] ${label}\n  JS:     ${a}\n  Python: ${e}`);
  }
}

// ---- dates ----
const dateCases = [
  "01/06/2026",
  "Date: 07/07/26",
  "DATE: 2026-09-23",
  "August 7, 2026",
  "22-Aug-2026 16:31:16 HRS",
  "8/19/26",
  "Jul 10, 2026 ... later Cash\n7/10/26 8:50 PM",
  "no date here at all",
  "31/02/2026",  // invalid date
];
for (const text of dateCases) {
  const jsResult = findAllDates(text).map((d) => d.iso);
  const pyResult = pyDates(text);
  check(`dates: ${JSON.stringify(text).slice(0, 50)}`, jsResult, pyResult);
}

// ---- money ----
const amountCases = ["₹2,000", "Rs.2,000.00", "Rs. 2000", "INR 2,000", "16,845.00", "(-) 0.00", "not a number", "", "1,25,000.00"];
for (const text of amountCases) {
  check(`amount: ${JSON.stringify(text)}`, parseAmount(text), pyAmount(text));
}

const amountsMatchCases = [
  [4499, 4499.77],
  [4500, 4499.77],
  [4498, 4499.77],
  [4501, 4499.77],
  [2000, 2000],
  [2000, 2000.0],
  [1999, 2000.0],
];
for (const [claimed, bill] of amountsMatchCases) {
  check(`amountsMatch(${claimed}, ${bill})`, amountsMatch(claimed, bill), pyAmountsMatch(claimed, bill));
}

// ---- category normalization ----
const categoryCases = [
  "BATTA [1]",
  "LODGING (LODGING)",
  "own car petrol",
  "  CAB CHARGES  ",
  null,
  "",
  "Toll Tax Charges",
  "SERVICE PURCHASES",  // deliberately NOT exact - should fail membership
];
for (const text of categoryCases) {
  check(`normalizeCategory(${JSON.stringify(text)})`, normalizeCategory(text), pyNormalizeCategory(text));
  check(`isPolicyMasterCategory(${JSON.stringify(text)})`, isPolicyMasterCategory(text), pyIsPolicyMasterCategory(text));
  check(`expectedBillLabel(${JSON.stringify(text)})`, expectedBillLabel(text), pyExpectedBillLabel(text));
}

console.log(`\n${pass} passed, ${fail} failed`);
process.exit(fail > 0 ? 1 : 0);
