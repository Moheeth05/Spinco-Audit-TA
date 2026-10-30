// Stage 4 parity harness, part 1: the pure logic pieces (heuristic
// functions) vs Python - these should be exact, since they're regex/string
// logic with no PDF-library involvement.
import { execFileSync } from "node:child_process";
import { expenseRowsLookWellFormed, textHasLabelledValues } from "./src/textExtraction.mjs";

const PYTHON = "D:\\Bills\\backend\\.venv\\Scripts\\python.exe";

function toPy(value) {
  if (value === null || value === undefined) return "None";
  return JSON.stringify(value);
}

function pyExpenseRowsLookWellFormed(text) {
  const script = `
import sys, json
sys.path.insert(0, r"D:\\Bills\\backend")
from app.services.extraction.text_extraction import _expense_rows_look_well_formed
print(json.dumps(_expense_rows_look_well_formed(${toPy(text)})))
`;
  return JSON.parse(execFileSync(PYTHON, ["-I", "-c", script], { encoding: "utf-8" }));
}

function pyTextHasLabelledValues(text) {
  const script = `
import sys, json
sys.path.insert(0, r"D:\\Bills\\backend")
from app.services.extraction.text_extraction import _text_has_labelled_values
print(json.dumps(_text_has_labelled_values(${toPy(text)})))
`;
  return JSON.parse(execFileSync(PYTHON, ["-I", "-c", script], { encoding: "utf-8" }));
}

let pass = 0, fail = 0;
function check(label, actual, expected) {
  if (JSON.stringify(actual) === JSON.stringify(expected)) pass++;
  else {
    fail++;
    console.log(`[FAIL] ${label}\n  JS: ${JSON.stringify(actual)}\n  Py: ${JSON.stringify(expected)}`);
  }
}

const rowCases = [
  "1.\n01/01/2026\nBATTA\nRs.100.00",
  "1.\n2.\n3.\n4.\n5.\n01/01/2026", // all row markers, no nearby dates - not well formed
  "1.\n01/01/2026\n2.\nNo date here at all\nRs.50.00\n3.\n02/01/2026\n4.\n03/01/2026\n5.\n04/01/2026", // 4/5 = 80%, at threshold
  "No row markers here at all",
  "",
];
for (const text of rowCases) {
  check(`expenseRowsLookWellFormed: ${JSON.stringify(text).slice(0, 40)}`, expenseRowsLookWellFormed(text), pyExpenseRowsLookWellFormed(text));
}

const labelCases = [
  "Catering Charges (Incl. of GST)\n₹ 174.00",
  "Total\nRs.2,329.65",
  "A clerkage charge of Rs.60 per passenger is applicable for cancellation within 4 hours of departure.",
  "Amount\nno value here",
  "Balance: Rs.500.00",
];
for (const text of labelCases) {
  check(`textHasLabelledValues: ${JSON.stringify(text).slice(0, 40)}`, textHasLabelledValues(text), pyTextHasLabelledValues(text));
}

console.log(`\n${pass} passed, ${fail} failed`);
process.exit(fail > 0 ? 1 : 0);
