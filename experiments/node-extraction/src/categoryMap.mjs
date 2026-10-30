// Port of backend/app/services/category_map.py - shared expense-category
// normalization and category<->bill-evidence mapping. One source of truth
// here too, mirroring the Python module's own role, so billParser.mjs and
// expenseReportParser.mjs never drift apart on category logic.
//
// Experimental / reference only - see README.md. Never wired into the
// production backend or Electron app.

import { parseAmount } from "./money.mjs";

// The full, authoritative 29-category Policy Master list (verbatim names,
// matching app/seed.py's POLICY_CATEGORIES on the Python side).
export const POLICY_MASTER_CATEGORIES = new Set([
  "AIRFARE",
  "BATTA",
  "CAB CHARGES",
  "CALIBRATION CHARGE",
  "COMPANY CAR MAINTENANCE",
  "COMPANY CAR PETROL",
  "COURIER CHARGES",
  "INTERNET CHARGES",
  "LOCAL CONVEYANCE",
  "LODGING",
  "MARKETING - SEMINAR EXPS",
  "MARKETING EXPENSES - FOOD",
  "MARKETING EXPENSES - OTHERS",
  "MEMBERSHIP & SUBSCRIPTION FEE",
  "OFFICE EXPENSES - DRIVER SALARY",
  "OWN CAR PETROL",
  "PRINTING & STATIONERY",
  "RATES & TAXES",
  "REPAIRS - COMPUTERS",
  "SERVICE PURCHASES/SERVICE CHARGE",
  "STAFF WELFARE",
  "TELEPHONE ACCESSORIES",
  "TENDER FEE",
  "TOLL TAX CHARGES",
  "TRAIN / BUS FARE",
  "TRANSPORT CHARGES",
  "TRAVEL-OTHERS",
  "TWO WHEELER MAINTENANCE",
  "TWO WHEELER PETROL",
]);

export const CATEGORY_TO_BILL_LABEL = {
  "OWN CAR PETROL": "Fuel",
  "COMPANY CAR PETROL": "Fuel",
  "TWO WHEELER PETROL": "Fuel",
  AIRFARE: "Flight",
  LODGING: "Lodging",
  "CAB CHARGES": "Cab/Taxi",
  "LOCAL CONVEYANCE": "Cab/Taxi",
  "STAFF WELFARE": "Food",
  "MARKETING EXPENSES - FOOD": "Food",
  "COURIER CHARGES": "Courier",
  "PRINTING & STATIONERY": "Printing & Stationery",
  "TRAIN / BUS FARE": "Train",
  "TRANSPORT CHARGES": "Freight/Transport",
  "TOLL TAX CHARGES": "Toll",
  "CALIBRATION CHARGE": "Calibration",
  "INTERNET CHARGES": "Internet",
  "TELEPHONE ACCESSORIES": "Phone Accessories",
  "REPAIRS - COMPUTERS": "Computer Repair",
  "RATES & TAXES": "Statutory Tax",
  "MEMBERSHIP & SUBSCRIPTION FEE": "Membership",
  "TENDER FEE": "Tender Fee",
  "TRAVEL-OTHERS": "Travel Miscellaneous",
  "MARKETING - SEMINAR EXPS": "Seminar/Conference",
  "MARKETING EXPENSES - OTHERS": "Marketing Collateral",
  // Deliberately last-priority at the CATEGORY_KEYWORDS matching level in
  // billParser.mjs, not here - this map is just category->label, order is
  // the matcher's concern.
  "SERVICE PURCHASES/SERVICE CHARGE": "Service Purchase",
};

const BRACKET_SUFFIX_RE = /\s*\[\d+\]\s*$/;
const TRAILING_PAREN_RE = /^(.*?)\s*\(([^)]+)\)\s*$/;

export function normalizeCategory(category) {
  if (!category) return null;
  let text = category.replace(BRACKET_SUFFIX_RE, "").trim();
  const m = TRAILING_PAREN_RE.exec(text);
  if (m && m[1].trim().toUpperCase() === m[2].trim().toUpperCase()) {
    text = m[1].trim();
  }
  const upper = text.trim().toUpperCase();
  return upper || null;
}

export function isPolicyMasterCategory(text) {
  const normalized = normalizeCategory(text);
  return normalized !== null && POLICY_MASTER_CATEGORIES.has(normalized);
}

export function expectedBillLabel(expenseCategory) {
  const normalized = normalizeCategory(expenseCategory);
  return normalized !== null ? CATEGORY_TO_BILL_LABEL[normalized] ?? null : null;
}

export function categoryCompatible(expenseCategory, billCategoryGuess) {
  if (!billCategoryGuess) return false;
  const expected = expectedBillLabel(expenseCategory);
  if (expected === null) return false;
  if (billCategoryGuess === expected) return true;
  if (expected === "Train" && billCategoryGuess === "Bus") return true;
  if (expected === "Bus" && billCategoryGuess === "Train") return true;
  return false;
}

const CATERING_RE = /\bcatering\b/gi;
const AMOUNT_NEAR_RE = /(?:₹|Rs\.?|INR)\s*[\d,]+(?:\.\d{1,2})?|\b\d[\d,]*\.\d{2}\b/i;

export function mentionsCatering(rawText) {
  return /\bcatering\b/i.test(rawText || "");
}

export function detectCateringAmount(rawText) {
  const text = rawText || "";
  CATERING_RE.lastIndex = 0;
  let m;
  while ((m = CATERING_RE.exec(text)) !== null) {
    const window = text.slice(m.index + m[0].length, m.index + m[0].length + 40);
    const amountMatch = AMOUNT_NEAR_RE.exec(window);
    if (amountMatch) return parseAmount(amountMatch[0]);
    if (m[0] === "") CATERING_RE.lastIndex++;
  }
  return null;
}
