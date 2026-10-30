// Regression tests for the multi-transaction-statement amount fix (see
// billParser.mjs's detectMultiTransactionStatement and the module comment
// on its module-level constants for the full rationale). These are NOT
// Node-vs-Python parity tests - fixing this intentionally makes Node
// DIVERGE from Python's current (still-buggy) behavior, confirmed by the
// real failures in reports/receipt-extraction-baseline-20261013.json. The
// goal is correct document interpretation, not matching Python.
import { readFileSync } from "node:fs";
import { parseBillText, detectMultiTransactionStatement } from "./src/billParser.mjs";

let pass = 0, fail = 0;
function check(label, actual, expected) {
  const a = JSON.stringify(actual);
  const e = JSON.stringify(expected);
  if (a === e) pass++;
  else {
    fail++;
    console.log(`[FAIL] ${label}\n  actual:   ${a}\n  expected: ${e}`);
  }
}

// ---------- Case 1: real credit-card statement (confirmed real failure) ----------
const ccStatement = readFileSync("fixtures/statement-samples/credit-card-statement-ER-40706-p20.txt", "utf-8");
{
  const detection = detectMultiTransactionStatement(ccStatement);
  check("case1 credit-card statement: detected as statement", detection.isStatement, true);
  const bill = parseBillText(ccStatement);
  check("case1 credit-card statement: amount unresolved (was 113824.75 before this fix)", bill.amount, null);
  check("case1 credit-card statement: isMultiTransactionStatement flag set", bill.isMultiTransactionStatement, true);
  check("case1 credit-card statement: 113,824.75 (the card balance) is never the amount", bill.amount !== 113824.75, true);
}

// ---------- Case 2: real FASTag statement (confirmed real failure) ----------
const fastagStatement = readFileSync("fixtures/statement-samples/fastag-statement-ER-40706-p22.txt", "utf-8");
{
  const detection = detectMultiTransactionStatement(fastagStatement);
  check("case2 FASTag statement: detected as statement", detection.isStatement, true);
  const bill = parseBillText(fastagStatement);
  check("case2 FASTag statement: amount unresolved", bill.amount, null);
  check("case2 FASTag statement: isMultiTransactionStatement flag set", bill.isMultiTransactionStatement, true);
}

// ---------- Case 3: minimal synthetic - amount due + minimum amount due pair alone ----------
{
  const text = "Minimum Amount due\nRs.700.00\nTotal Amount due\nRs.14,500.00\nDue Date: 15/08/2026";
  const detection = detectMultiTransactionStatement(text);
  check("case3 minimal 'amount due' pair: detected as statement (no date-count needed)", detection.isStatement, true);
  const bill = parseBillText(text);
  check("case3 minimal 'amount due' pair: amount unresolved, not 14500", bill.amount, null);
}

// ---------- Case 4: single-transaction payment screenshot (must not be flagged) ----------
{
  const text = "Transaction Successful\n06:19 PM on 21 Sep 2026\nPaid to\nPANKAJ BALDEVBHAI PARMAR\npaparmar9393@okaxis\nMessage\nCab charges\nPhonePe Transaction ID\nT2609211819064088081730";
  const detection = detectMultiTransactionStatement(text);
  check("case4 payment screenshot: NOT a statement", detection.isStatement, false);
  const bill = parseBillText(text);
  check("case4 payment screenshot: isMultiTransactionStatement flag false", bill.isMultiTransactionStatement, false);
}

// ---------- Case 5: fuel receipt with multiple numeric fields (regression - must still resolve) ----------
{
  const text = "HP\nWelcomes You\nDate: 14/07/26\nProduct: Petrol\nDensity: 745.5Kg/Cu.mtr\nRate(Rs/L): 116.38\nVolume(L): 33.58\nAmount(Rs): 3908.04";
  const detection = detectMultiTransactionStatement(text);
  check("case5 fuel receipt: NOT a statement despite several numeric fields", detection.isStatement, false);
  const bill = parseBillText(text);
  check("case5 fuel receipt: amount still correctly resolves to 3908.04 (rate/density/volume not mistaken for it)", bill.amount, 3908.04);
}

// ---------- Case 6: multi-day invoice, multiple items + tax + one payable total (the explicit false-positive guard) ----------
// 6 distinct dates (above the 5-date threshold used on the statement-heading
// path) but ZERO statement terminology - this is the exact scenario the task
// required NOT be misclassified, and the exact real counter-example
// (Home Town Resort, ER-40706.pdf pages 10-11) that motivated requiring
// terminology, never dates alone.
{
  const text = [
    "TAX INVOICE",
    "Home Town Resort",
    "Date V.No Room Description Debit Credit Balance",
    "30/06/2026 ROOM CHARGES #103 3333.33",
    "01/07/2026 ROOM CHARGES #103 3333.33",
    "02/07/2026 ROOM CHARGES #103 3333.33",
    "03/07/2026 ROOM CHARGES #103 3333.33",
    "04/07/2026 ROOM CHARGES #103 3333.33",
    "05/07/2026 ROOM CHARGES #103 3333.33",
    "CGST@2.5%: 499.99",
    "SGST@2.5%: 499.99",
    "Net Amount: Rs.20999.97",
  ].join("\n");
  const detection = detectMultiTransactionStatement(text);
  check("case6 multi-day invoice (6 dates, no statement terms): NOT a statement", detection.isStatement, false);
  const bill = parseBillText(text);
  check("case6 multi-day invoice: Net Amount still correctly resolves", bill.amount, 20999.97);
}

// ---------- Case 7: statement-labeled document with a clearly identifiable individual transaction ----------
// Has a weak "statement" heading but NOT the balance-pair phrases, and only
// 1 date - stays below the date-count threshold, so the document's own
// clearly-labeled amount is preserved rather than suppressed.
{
  const text = "Account Statement\nDate: 01/01/2026\nAmount Paid: Rs.500.00";
  const detection = detectMultiTransactionStatement(text);
  check("case7 statement heading + single identifiable transaction: NOT suppressed", detection.isStatement, false);
  const bill = parseBillText(text);
  check("case7 statement heading + single identifiable transaction: amount preserved", bill.amount, 500);
}

// ---------- Case 8: ambiguous multi-transaction document - amount must remain unresolved ----------
// (the real credit-card statement, case 1, already covers this directly -
// re-asserted here under the task's own case-8 naming for traceability)
{
  const bill = parseBillText(ccStatement);
  check("case8 ambiguous statement: amount is null, not guessed", bill.amount, null);
  check("case8 ambiguous statement: status reflects unresolved amount", bill.status, "REVIEW_REQUIRED");
}

// ---------- Case 9: ordinary receipts without statement-specific terminology (broad regression sweep) ----------
const ordinaryReceipts = [
  "Air India Express\nBoarding Pass\nAirline Fuel Charge: Rs. 299\nTotal Fare: Rs.14,356.00\nDate of Travel: 11/09/2026",
  "IndianOil\nHigh Speed Diesel\nRate(Rs/L): 94.50\nAmount: Rs.2,468.40\nDate: 22/04/2026",
  "GLOBAL CONSULTING SERVICES PVT LTD\nGSTIN: 29ABCDE1234F1Z5\nTotal Amount: Rs.29,500.00\nDate: 15/03/2026",
  // Explicit subtotal/tax/total breakdown (multiple amounts, no dates, no
  // statement terms) - must resolve to Total, not Subtotal, and must not
  // be misclassified as a statement.
  "KFC Order Summary\nSubtotal 600.0\nCGST 15.0\nSGST 15.0\nTotal 630.0",
];
for (const text of ordinaryReceipts) {
  const detection = detectMultiTransactionStatement(text);
  check(`case9 ordinary receipt not flagged: ${JSON.stringify(text).slice(0, 40)}`, detection.isStatement, false);
}
{
  const bill = parseBillText(ordinaryReceipts[3]);
  check("case9 subtotal/tax/total receipt: resolves to Total (630), not Subtotal (600)", bill.amount, 630);
}

console.log(`\n${pass} passed, ${fail} failed`);
process.exit(fail > 0 ? 1 : 0);
