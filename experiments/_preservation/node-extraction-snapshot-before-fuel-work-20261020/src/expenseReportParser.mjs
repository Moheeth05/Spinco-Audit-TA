// Full-parity port of backend/app/services/extraction/expense_report_parser.py
// - including this session's P0 fix (category line identified by Policy
// Master membership, not position, so an open-ended set of optional
// "Label : value" prefix lines - Merchant, Ref#, Expense Location, PNR
// Number, Attendees, ... - never corrupts the category field).
//
// Experimental / reference only - see README.md. Never wired into the
// production backend or Electron app.

import { isPolicyMasterCategory } from "./categoryMap.mjs";
import { parseDate } from "./dates.mjs";
import { parseAmount } from "./money.mjs";

const ER_ID_RE = /^(ER-\d+)$/;
const ER_ID_FALLBACK_RE = /\b(ER-\d+)\b/;
const TRIP_NUMBER_RE = /Trip Number:\s*(TRIP-\d+)/i;
const AMOUNT_ONLY_RE = /^(?:₹|Rs\.?|INR)\s*[\d,]+(?:\.\d+)?$/i;
const ROW_START_RE = /^(\d+)\.$/;
const SIGNATURE_RE = /^([A-Z][A-Za-z .]+?)\s*\(\s*([A-Z]{1,6}\d{1,6})\s*\)$/;
const MERCHANT_LINE_RE = /^Merchant\s*:\s*(.+)$/i;
const REFERENCE_LINE_RE = /^Ref(?:erence)?#?\s*:\s*(.+)$/i;
const MAX_PRE_CATEGORY_LINES = 8;

function flatten(pages) {
  const flat = [];
  for (const page of pages) {
    for (const line of page.text.split("\n")) {
      flat.push([page.pageNumber, line]);
    }
  }
  return flat;
}

export function findReportSummaryPage(pages) {
  for (const page of pages) {
    if (page.text.toUpperCase().includes("REPORT SUMMARY")) return page.pageNumber;
  }
  return null;
}

function nextNonblank(flat, start) {
  let i = start;
  while (i < flat.length && !flat[i][1].trim()) i++;
  if (i < flat.length) return [flat[i][0], flat[i][1].trim(), i];
  return [null, null, i];
}

export function extractHeader(pages) {
  const flat = flatten(pages);
  const fullText = flat.map(([, line]) => line).join("\n");
  const warnings = [];

  const result = {
    erId: null, erIdPage: null,
    employeeName: null, employeeNamePage: null,
    employeeCode: null, employeeCodePage: null,
    submittedOn: null, submittedOnPage: null,
    totalClaimed: null, totalClaimedPage: null,
    tripNumber: null, tripNumberPage: null,
  };

  for (let idx = 0; idx < flat.length; idx++) {
    if (flat[idx][1].trim() === "Expense Report") {
      const [p, val] = nextNonblank(flat, idx + 1);
      if (val && ER_ID_RE.test(val)) {
        result.erId = val;
        result.erIdPage = p;
      }
      break;
    }
  }
  if (!result.erId) {
    const m = ER_ID_FALLBACK_RE.exec(fullText);
    if (m) {
      result.erId = m[1];
      warnings.push("ER ID recovered via fallback search rather than the expected header position.");
    } else {
      warnings.push("Expense Report ID could not be found in the document.");
    }
  }

  for (let idx = 0; idx < flat.length; idx++) {
    if (flat[idx][1].trim() === "Amount to be Reimbursed") {
      const [p, val] = nextNonblank(flat, idx + 1);
      const amt = parseAmount(val);
      if (amt !== null) {
        result.totalClaimed = amt;
        result.totalClaimedPage = p;
      }
      break;
    }
  }

  for (let idx = 0; idx < flat.length; idx++) {
    if (flat[idx][1].trim() === "Total Reimbursable Amount") {
      const [p, val] = nextNonblank(flat, idx + 1);
      const amt = parseAmount(val);
      if (amt !== null) {
        result.totalClaimed = amt;
        result.totalClaimedPage = p;
      }
      break;
    }
  }
  if (result.totalClaimed === null) warnings.push("Total claimed amount could not be reliably extracted.");

  for (let idx = 0; idx < flat.length; idx++) {
    if (flat[idx][1].trim() === "Submitted By") {
      const [p, val] = nextNonblank(flat, idx + 1);
      if (val && !val.includes("@")) {
        result.employeeName = val;
        result.employeeNamePage = p;
      }
      break;
    }
  }
  if (!result.employeeName) warnings.push("Employee name could not be reliably extracted.");

  for (let idx = 0; idx < flat.length; idx++) {
    if (flat[idx][1].trim() === "Submitted On") {
      const [p, val] = nextNonblank(flat, idx + 1);
      const d = parseDate(val);
      if (d) {
        result.submittedOn = d;
        result.submittedOnPage = p;
      }
      break;
    }
  }

  const namePrefix = result.employeeName ? result.employeeName.toUpperCase().slice(0, 8) : null;
  for (const [pageNum, line] of flat) {
    const m = SIGNATURE_RE.exec(line.trim());
    if (m) {
      const name = m[1].trim(), code = m[2].trim();
      if (!namePrefix || name.toUpperCase().startsWith(namePrefix)) {
        result.employeeCode = code;
        result.employeeCodePage = pageNum;
        break;
      }
    }
  }

  const tripMatch = TRIP_NUMBER_RE.exec(fullText);
  if (tripMatch) {
    for (const [pageNum, line] of flat) {
      if (TRIP_NUMBER_RE.test(line)) {
        result.tripNumber = tripMatch[1];
        result.tripNumberPage = pageNum;
        break;
      }
    }
  }

  result.warnings = warnings;
  return result;
}

export function extractExpenseLines(pages) {
  const flat = flatten(pages);
  const rows = [];
  const warnings = [];
  let inTable = false;
  let i = 0;
  const n = flat.length;

  while (i < n) {
    const [pageNum, rawLine] = flat[i];
    const line = rawLine.trim();

    if (line === "EXPENSE SUMMARY") {
      inTable = true;
      i++;
      continue;
    }
    if (inTable && line.startsWith("REPORT SUMMARY")) break;

    const rowStartMatch = ROW_START_RE.exec(line);
    if (inTable && rowStartMatch) {
      const rowNo = parseInt(rowStartMatch[1], 10);
      const rowPage = pageNum;
      i++;

      let [, dateText] = nextNonblank(flat, i);
      let [, , dateIdx] = nextNonblank(flat, i);
      const expenseDate = parseDate(dateText);
      if (expenseDate === null) warnings.push(`Row ${rowNo}: could not parse a date from '${dateText}'.`);
      i = dateIdx + 1;

      let [catPage, category, idx] = nextNonblank(flat, i);
      let merchant = null, merchantPage = null;
      let reference = null, referencePage = null;
      const preCategoryLines = [];

      while (category !== null) {
        if (ROW_START_RE.test(category) || AMOUNT_ONLY_RE.test(category)) {
          category = null;
          break;
        }
        let m = MERCHANT_LINE_RE.exec(category);
        if (m) {
          merchant = m[1].trim();
          merchantPage = catPage;
          i = idx + 1;
          [catPage, category, idx] = nextNonblank(flat, i);
          continue;
        }
        m = REFERENCE_LINE_RE.exec(category);
        if (m) {
          reference = m[1].trim();
          referencePage = catPage;
          i = idx + 1;
          [catPage, category, idx] = nextNonblank(flat, i);
          continue;
        }
        if (isPolicyMasterCategory(category)) {
          i = idx + 1;
          break;
        }
        if (preCategoryLines.length >= MAX_PRE_CATEGORY_LINES) {
          category = null;
          break;
        }
        preCategoryLines.push(category);
        i = idx + 1;
        [catPage, category, idx] = nextNonblank(flat, i);
      }

      const detailLines = [...preCategoryLines];
      let amount = null;
      let amountPage = rowPage;
      let foundAmount = false;
      while (i < n) {
        const [p2, l2] = flat[i];
        const s2 = l2.trim();
        if (!s2) {
          i++;
          continue;
        }
        if (ROW_START_RE.test(s2)) {
          warnings.push(`Row ${rowNo}: amount not found before the next row started.`);
          break;
        }
        if (AMOUNT_ONLY_RE.test(s2)) {
          amount = parseAmount(s2);
          amountPage = p2;
          foundAmount = true;
          i++;
          break;
        }
        detailLines.push(s2);
        i++;
      }
      if (!foundAmount) warnings.push(`Row ${rowNo}: claimed amount could not be reliably extracted.`);

      const details = detailLines.join(" ").trim() || null;
      const isComplete = expenseDate !== null && category !== null && amount !== null;
      rows.push({
        sNo: rowNo,
        expenseDate,
        category,
        merchant,
        referenceId: reference,
        expenseDetails: details,
        claimedAmount: amount,
        sourcePage: rowPage,
        amountSourcePage: amountPage,
        status: isComplete ? "EXTRACTED" : "REVIEW_REQUIRED",
      });
      continue;
    }
    i++;
  }

  if (!rows.length) warnings.push("No expense line items could be found in the EXPENSE SUMMARY section.");
  return [rows, warnings];
}

export function extractEmbeddedTripSummary(pages) {
  const flat = flatten(pages);
  const hasSection = flat.some(([, line]) => line.trim() === "TRIP SUMMARY");
  if (!hasSection) return null;

  const fullText = flat.map(([, line]) => line).join("\n");
  let tripNumber = null, tripNumberPage = null;
  const tripMatch = TRIP_NUMBER_RE.exec(fullText);
  if (tripMatch) {
    for (const [pageNum, line] of flat) {
      if (TRIP_NUMBER_RE.test(line)) {
        tripNumber = tripMatch[1];
        tripNumberPage = pageNum;
        break;
      }
    }
  }

  let city = null, cityPage = null;
  let inSection = false;
  let sourcePage = null;
  const pairs = [];
  let pendingLoc = null;

  for (const [pageNum, rawLine] of flat) {
    const line = rawLine.trim();
    if (line === "TRIP SUMMARY") {
      inSection = true;
      sourcePage = pageNum;
      continue;
    }
    if (!inSection) continue;
    if (line === "EXPENSE SUMMARY") break;
    if (line.toLowerCase().startsWith("city:")) {
      city = line.split(":").slice(1).join(":").trim();
      cityPage = pageNum;
      continue;
    }
    if (line === "From" || line === "To" || line === "Description" || !line) continue;

    const d = parseDate(line);
    if (d !== null) {
      if (pendingLoc !== null) {
        const [loc, p] = pendingLoc;
        pairs.push([loc, d, p]);
        pendingLoc = null;
      }
    } else {
      pendingLoc = [line, pageNum];
    }
  }

  const legs = [];
  for (let idx = 0; idx < pairs.length - 1; idx += 2) {
    const [fromLoc, fromDate] = pairs[idx];
    const [toLoc, toDate] = pairs[idx + 1];
    legs.push({
      legNo: idx / 2 + 1,
      fromLocation: fromLoc,
      fromDate,
      toLocation: toLoc,
      toDate,
      description: null,
    });
  }

  return {
    tripNumber,
    tripNumberPage,
    destination: city,
    destinationPage: cityPage,
    sourcePage,
    legs,
  };
}
