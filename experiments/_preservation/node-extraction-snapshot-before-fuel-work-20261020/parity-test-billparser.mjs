// Stage 2 parity harness: billParser.mjs vs the real bill_parser.py, via
// the project's own Python venv - genuine comparison, not eyeballed.
import { execFileSync } from "node:child_process";
import {
  parseBillText,
  hasBillEvidence,
  hasConfidentVendorLine,
  splitBillSegments,
} from "./src/billParser.mjs";

const PYTHON = "D:\\Bills\\backend\\.venv\\Scripts\\python.exe";

function toPy(value) {
  if (value === null || value === undefined) return "None";
  if (value === true) return "True";
  if (value === false) return "False";
  return JSON.stringify(value);
}

function pyParseBillText(text) {
  const script = `
import sys, json
sys.path.insert(0, r"D:\\Bills\\backend")
from app.services.extraction.bill_parser import parse_bill_text
b = parse_bill_text(${toPy(text)})
print(json.dumps({
    "vendor": b.vendor,
    "billNumber": b.bill_number,
    "billDate": b.bill_date.isoformat() if b.bill_date else None,
    "amount": b.amount,
    "gstin": b.gstin,
    "categoryGuess": b.category_guess,
    "confidence": b.confidence,
    "status": b.status,
}))
`;
  return JSON.parse(execFileSync(PYTHON, ["-I", "-c", script], { encoding: "utf-8" }));
}

function pyHasBillEvidence(text) {
  const script = `
import sys, json
sys.path.insert(0, r"D:\\Bills\\backend")
from app.services.extraction.bill_parser import has_bill_evidence
print(json.dumps(has_bill_evidence(${toPy(text)})))
`;
  return JSON.parse(execFileSync(PYTHON, ["-I", "-c", script], { encoding: "utf-8" }));
}

function pyHasConfidentVendorLine(text) {
  const script = `
import sys, json
sys.path.insert(0, r"D:\\Bills\\backend")
from app.services.extraction.bill_parser import has_confident_vendor_line
print(json.dumps(has_confident_vendor_line(${toPy(text)})))
`;
  return JSON.parse(execFileSync(PYTHON, ["-I", "-c", script], { encoding: "utf-8" }));
}

function pySplitBillSegments(text) {
  const script = `
import sys, json
sys.path.insert(0, r"D:\\Bills\\backend")
from app.services.extraction.bill_parser import split_bill_segments
print(json.dumps(split_bill_segments(${toPy(text)})))
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

// Real-world-shaped bill texts covering: every newly-researched category,
// every false-positive guard added this session, the OCR-fuzzy fallback,
// vendor extraction (strong/weak/continuation-skip), arithmetic/loose
// amount fallbacks, and multi-GSTIN segment splitting.
const billCases = [
  // Flight-before-Fuel guard (Airline Fuel Charge must not read as Fuel)
  "Air India Express\nBoarding Pass\nAirline Fuel Charge: Rs. 299\nTotal Fare: Rs.14,356.00\nDate of Travel: 11/09/2026",
  // Fuel brand name, unguarded
  "IndianOil\nHigh Speed Diesel\nRate(Rs/L): 94.50\nAmount: Rs.2,468.40\nDate: 22/04/2026",
  // PETROLEUM guarded by dispenser context
  "UNNATI PETROLEUM\nNozzle No.: 01\nProduct: Diesel\nRate(Rs/L): 97.92\nAmount: Rs.3,900.15\nDate : 07/07/26",
  // Real confirmed OCR defect: fused leading char + inserted space
  "Receipt No: 098\n\u00c5Nozzle\nNo. :01\nProduct\nDensity\n: Diese l\nAmount(Rs): 03900.15",
  // Hotel evidence via FOLIO (brand-suffix words deliberately not trusted)
  "SUNRISE GRAND RESIDENCY\nGSTIN: 07XYZAB5678C1Z9\nFolio No: F-88213\nRoom Tariff: Rs.3,500.00\nTotal Amount: Rs.8,260.00\nCheck-In: 10/05/2026",
  // Marketing-before-Printing guard
  "Flex Banner Printing\nHoarding Charges: Rs.15,000.00\nDate: 01/06/2026",
  // Computer-Repair-before-Hardware guard
  "Dell Authorized Service Center\nHardware Repair Invoice\nDiagnostic Fee: Rs.250.00\nDate: 05/07/2026",
  // Toll
  "NHAI Toll Plaza\nFASTag Transaction\nAmount: Rs.65.00\nDate: 03/06/2026",
  // Freight/Transport (goods, not passenger bus)
  "Goods Transport Agency\nConsignment Note No: GTA-4521\nFreight: Rs.5,000.00\nDate: 01/01/2026",
  // Calibration
  "NABL Accredited Laboratory\nCalibration Certificate No: CC-2024-001\nAmount: Rs.1,200.00\nDate: 10/07/2026",
  // Internet (phrase-anchored, not bare)
  "Airtel Xstream Fiber\nBroadband Bill\nPlan rental: Rs.999.00\nDate: 10/09/2026",
  // Phone accessories (vs mobile recharge must not match - tested separately)
  "Amazon.in Tax Invoice\nMobile Charger Type-C\nQty: 1\nHSN 8504\nAmount: Rs.399.00\nDate: 01/01/2026",
  // Statutory tax
  "Municipal Corporation\nProperty Tax Receipt\nAssessment Year 2026-27\nAmount: Rs.5,000.00\nDate: 01/04/2026",
  // Membership
  "ICAI Annual Fee Circular\nMembership Fee Receipt\nFY 2026-27\nAmount: Rs.2,500.00\nDate: 01/04/2026",
  // Tender fee (EMD must not match - tested separately)
  "Notice Inviting Tender\nTender Fee (DD/BC in original): Rs 1180\nDate: 01/01/2026",
  // Travel miscellaneous
  "VFS Global\nVisa Fee Receipt\nApplication Number: 55213\nAmount: Rs.3,500.00\nDate: 01/02/2026",
  // Seminar/Conference
  "Conference Registration Fee\nDelegate Kit Included\nAmount: Rs.5,000.00\nDate: 01/03/2026",
  // Service Purchase (fallback-only)
  "Annual Maintenance Contract\nService Charges for Q1: Rs.8,000.00\nDate: 01/01/2026",
  // Vendor: strong suffix match
  "GLOBAL CONSULTING SERVICES PVT LTD\nGSTIN: 29ABCDE1234F1Z5\nTotal Amount: Rs.29,500.00\nDate: 15/03/2026",
  // Vendor: weak fallback, continuation marker correctly skipped
  "Continued from page 1\nSubtotal: Rs.40,000.00\nTotal Amount: Rs.47,200.00",
  // Amount: arithmetic sum fallback (no labeled total)
  "Ride fare 520\nCGST 13\nSGST 13\nDate: 01/01/2026",
  // Multi-GSTIN page (two bills on one page)
  "FIRST VENDOR SERVICES LTD\nGSTIN: 19AAAAA1111A1Z1\nAmount: Rs.1,000.00\n\n\nSECOND VENDOR TRADERS LTD\nGSTIN: 27BBBBB2222B1Z2\nAmount: Rs.2,000.00",
  // Genuinely novel service type - must stay unresolved
  "GREENLEAF PEST CONTROL SERVICES\nService Invoice\nService: Quarterly Pest Control Treatment\nDate: 10/07/2026\nAmount: Rs.1,200.00",
  // Empty / whitespace-only
  "   ",
  "",
];

for (const text of billCases) {
  const jsResult = parseBillText(text);
  const pyResult = pyParseBillText(text);
  check(`parseBillText: ${JSON.stringify(text).slice(0, 60)}`, {
    vendor: jsResult.vendor,
    billNumber: jsResult.billNumber,
    billDate: jsResult.billDate ? jsResult.billDate.iso : null,
    amount: jsResult.amount,
    gstin: jsResult.gstin,
    categoryGuess: jsResult.categoryGuess,
    confidence: jsResult.confidence,
    status: jsResult.status,
  }, pyResult);

  check(`hasBillEvidence: ${JSON.stringify(text).slice(0, 40)}`, hasBillEvidence(text), pyHasBillEvidence(text));
  check(`hasConfidentVendorLine: ${JSON.stringify(text).slice(0, 40)}`, hasConfidentVendorLine(text), pyHasConfidentVendorLine(text));
}

for (const text of billCases) {
  check(`splitBillSegments: ${JSON.stringify(text).slice(0, 40)}`, splitBillSegments(text), pySplitBillSegments(text));
}

console.log(`\n${pass} passed, ${fail} failed`);
process.exit(fail > 0 ? 1 : 0);
