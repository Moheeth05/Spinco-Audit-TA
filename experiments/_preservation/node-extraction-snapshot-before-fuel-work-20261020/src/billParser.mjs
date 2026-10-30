// Full-parity port of backend/app/services/extraction/bill_parser.py.
// Mirrors every regex, priority list, and piece of logic exactly - same
// CATEGORY_KEYWORDS (including this session's 12 newly-researched
// categories and every false-positive guard), same fuzzy OCR-noise
// fallback, same label/value window, same vendor/bill-number/GSTIN
// extraction, same multi-GSTIN page-splitting. The optional NVIDIA
// supplementary-fill pass is intentionally NOT ported - it requires a
// configured external API key, is a no-op by default in the real app (no
// key configured), and is out of scope for a from-the-PDF extraction
// comparison.
//
// Experimental / reference only - see README.md. Never wired into the
// production backend or Electron app.

import { findAllDates } from "./dates.mjs";
import { parseAmount } from "./money.mjs";

export const GSTIN_RE = /\b\d{2}[A-Z]{5}\d{4}[A-Z][A-Z\d]Z[A-Z\d]\b/g;

// (priority, keyword) - higher priority wins when multiple labeled amounts exist.
const AMOUNT_LABELS = [
  [5, "grand total"],
  [5, "total amount"],
  [4, "net amount"],
  [4, "amount payable"],
  [4, "total payable"],
  [4, "total fare"],
  [3, "total reimbursable"],
  [3, "amount"],
  [3, "total"],
  [2, "sub total"],
  [2, "subtotal"],
];
const AMOUNT_LABEL_PATTERNS = AMOUNT_LABELS.map(([priority, label]) => [
  priority,
  label,
  new RegExp(`\\b${escapeRegExp(label)}\\b`, "gi"),
]);

const BILL_NUMBER_LABELS = [
  "invoice no", "inv no", "inv.no", "bill no", "receipt no", "receipt",
  "txn id", "reference no", "ref no", "awb no", "memo#", "memo #",
];

const DATE_LABELS = [
  [4, "invoice date"], [4, "bill date"], [3, "date of travel"], [3, "journey date"],
  [3, "start date"], [2, "txn date"], [2, "booking date"], [1, "date"], [1, "dt"], [1, "txst"],
];
const DATE_EXCLUDE_LABELS = [
  "due date", "payment date", "print date", "printed date", "generated date",
  "upload date", "departure date", "arrival date", "expiry date", "valid until",
  "valid upto", "validity date",
];

export const VENDOR_SUFFIX_RE = new RegExp(
  "\\b(LTD|LIMITED|PVT|CORP|CORPORATION|HOTEL|RESTAURANT|TRAVELS|TOUR|STORES|AGENC\\w*|" +
    "PETROLEUM|EXPRESS|DEALER|CAFE|ENTERPRISE\\w*|HARDWARE\\w*|ELECTRICAL\\w*|SERVICES?|" +
    "BAR|AUTOFUELS?|MESS)\\b",
  "i"
);

// Same order, same guards, same 12 newly-researched categories as the
// Python CATEGORY_KEYWORDS - see bill_parser.py's own inline comments for
// the reasoning behind each ordering/guard decision; not re-duplicated
// here beyond a short pointer, to keep this file from drifting out of sync
// with those comments over time.
export const CATEGORY_KEYWORDS = [
  [/\b(AIR INDIA(?:\s+EXPRESS)?|INDIGO|SPICEJET|VISTARA|AKASA(?:\s+AIR)?|GO\s*FIRST|GOAIR|ALLIANCE AIR|BOARDING PASS|BAGGAGE|CABIN BAGGAGE|BOARDING TIME|DEPARTURE GATE)\b/i, "Flight"],
  [/\b(PETROL|DIESEL|AUTOFUEL|NOZZLE|NOZL)\b/i, "Fuel"],
  [/(?<!AIRLINE )\bFUEL\b(?!\s+STATION)/i, "Fuel"],
  [/\b(BHARAT PETROLEUM|HINDUSTAN PETROLEUM|INDIAN\s*OIL|IOCL|BPCL|HPCL|NAYARA|XTRAGREEN|\bHSD\b)\b/i, "Fuel"],
  [/\bPETROLEUM\b(?=[\s\S]{0,300}\b(?:NOZZLE|NOZL|RATE|VOLUME|LTR|LITRE|DENSITY|PRODUCT)\b)/i, "Fuel"],
  [/\b(HOTEL|LODGING|ROOM CHARGE|ACCOMMODATION|ROOM TARIFF|ROOM NO|GUEST NAME|CHECK-IN|CHECK-OUT|FOLIO|NIGHTS STAYED)\b/i, "Lodging"],
  [/\b(UBER|OLA|CAB|TAXI|RIDE FARE|RIDE|PASSENGERS?\s+TRANSPORT(?:\s+SERVICES?)?)\b/i, "Cab/Taxi"],
  [/\b(FOOD|RESTAURANT|MEAL|DINE|MESS|CAFE|BAR)\b/i, "Food"],
  [/\b(COURIER|DTDC|AWB)\b/i, "Courier"],
  [/\b(ADVERTISEMENT|ADVERTISING|PUBLICITY EXPENSES?|SPONSORSHIP|FLEX (?:BANNER|PRINTING)|HOARDING|BRANDED MERCHANDISE|CORPORATE GIFT|CLIENT GIFT|BROCHURE PRINTING|FLYER PRINTING|SIGNAGE)\b/i, "Marketing Collateral"],
  [/\b(PRINTING|STATIONERY|STATIONARY)\b/i, "Printing & Stationery"],
  [/\b(TRAIN|RAILWAY|IRCTC|PNR)\b/i, "Train"],
  [/\bBUS\b/i, "Bus"],
  [/\b(LAPTOP REPAIR|COMPUTER REPAIR|MOTHERBOARD REPAIR|SCREEN REPLACEMENT|HARDWARE REPAIR|IT SERVICE CENTER|AUTHORIZED SERVICE CENTER|LAPTOP SERVICING|CHIP-LEVEL REPAIR|DIAGNOSTIC FEE|DIAGNOSIS FEE)\b/i, "Computer Repair"],
  [/\b(ELECTRICAL|HARDWARE)\b/i, "Hardware/Electrical"],
  [/\b(TOLL PLAZA|TOLL TAX|TOLL FEE|FASTAG|NETC|\bNHAI\b)\b/i, "Toll"],
  [/\b(CONSIGNMENT NOTE|GOODS TRANSPORT AGENCY|\bGTA\b|LORRY RECEIPT|\bLR NO\b|TRUCK HIRE|VEHICLE HIRE CHARGES|CARTAGE|FREIGHT)\b/i, "Freight/Transport"],
  [/\b(CALIBRATION|NABL ACCREDITED|ISO[\s/]?IEC\s*17025|TRACEABILITY CERTIFICATE)\b/i, "Calibration"],
  [/\b(BROADBAND|WI-?FI|STATIC IP|LEASED LINE|FIBERNET|FIBRENET|JIO FIBER|AIRTEL XSTREAM|ACT FIBERNET|HATHWAY|BSNL BROADBAND|CONNECTIVITY SERVICES|INTERNET (?:BILL|CHARGES|SERVICES?|CONNECTION))\b/i, "Internet"],
  [/\b(MOBILE CHARGER|PHONE CHARGER|DATA CABLE|USB CABLE|EARPHONES?|EARBUDS|HEADSET|HEADPHONES?|POWER BANK|SCREEN GUARD|TEMPERED GLASS|PHONE CASE|MOBILE COVER|MOBILE STAND)\b/i, "Phone Accessories"],
  [/\b(PROPERTY TAX|PROFESSIONAL TAX|MUNICIPAL (?:CORPORATION )?TAX|TRADE LICEN[CS]E|FACTORY LICEN[CS]E|SHOP (?:AND|&) ESTABLISHMENT LICEN[CS]E|ROAD TAX)\b/i, "Statutory Tax"],
  [/\b(MEMBERSHIP FEE|ANNUAL MEMBERSHIP|MEMBERSHIP RENEWAL|\bICAI\b|\bICSI\b|\bIEEE\b|CERTIFICATE OF PRACTICE|\bCOP FEE\b|ANNUAL FEE CIRCULAR|ASSOCIATION DUES)\b/i, "Membership"],
  [/\b(TENDER FEE|TENDER DOCUMENT|COST OF TENDER|BID DOCUMENT FEE)\b/i, "Tender Fee"],
  [/\b(VISA FEE|VISA APPLICATION|TRAVEL INSURANCE|OVERSEAS MEDICLAIM|EXCESS BAGGAGE|BAGGAGE CHARGES|AIRPORT PARKING|LOUNGE ACCESS|CURRENCY CONVERSION|DYNAMIC CURRENCY CONVERSION|\bDCC\b|\bFOREX\b)\b/i, "Travel Miscellaneous"],
  [/\b(DELEGATE FEE|SEMINAR REGISTRATION|CONFERENCE REGISTRATION|WORKSHOP FEE|EXHIBITION STALL|TRADE FAIR|SHELL SCHEME|STALL (?:RENT|CHARGES)|BOOTH CHARGES|SPACE RENT)\b/i, "Seminar/Conference"],
  [/\b(SERVICE CHARGES?|SERVICE PURCHASES?|\bAMC\b|ANNUAL MAINTENANCE CONTRACT|PROFESSIONAL (?:CHARGES|FEES)|CONSULTANCY (?:CHARGES|FEES))\b/i, "Service Purchase"],
];

const FUZZY_CATEGORY_WORDS = [
  ["NOZZLE", "Fuel"], ["PETROL", "Fuel"], ["DIESEL", "Fuel"],
  ["HOTEL", "Lodging"], ["COURIER", "Courier"], ["STATIONERY", "Printing & Stationery"],
];

const BILL_EVIDENCE_RE = /\b(RECEIPT|INVOICE|TAX INVOICE|BILL|GSTIN|THANK YOU|CASH BILL|RETAIL INVOICE)\b/i;
const AMOUNT_TOKEN_RE = /(?:₹|Rs\.?|INR)\s*[\d,]+(?:\.\d{1,2})?|\b\d[\d,]*\.\d{2}\b/gi;
const LOOSE_NUMBER_RE = /\d[\d,]{1,6}/g;
const CONTINUATION_MARKER_RE = /\b(CONTINUED|CONTD\.?|CONTINUATION)\b|\bPAGE\s+\d+\s+OF\s+\d+\b/i;

const FLAT_WINDOW_CHARS = 40;
const MAX_VALUE_LOOKAHEAD_LINES = 3;
const FUZZY_AMOUNT_LABELS = ["amount", "total"];

function escapeRegExp(s) {
  return s.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
}

function matchAllTokens(re, text) {
  re.lastIndex = 0;
  const out = [];
  let m;
  while ((m = re.exec(text)) !== null) {
    out.push(m[0]);
    if (m[0] === "") re.lastIndex++;
  }
  return out;
}

export function hasBillEvidence(text) {
  return Boolean(text) && text.trim().length >= 5;
}

export function parsedHasEvidence(parsed, text) {
  if (parsed.amount !== null) return true;
  if (parsed.gstin || parsed.billNumber || parsed.categoryGuess) return true;
  return BILL_EVIDENCE_RE.test(text);
}

function labelValueWindow(text, idx, labelLen) {
  const lineEnd = text.indexOf("\n", idx);
  if (lineEnd === -1) return text.slice(idx, idx + labelLen + FLAT_WINDOW_CHARS);
  const ownLineRest = text.slice(idx + labelLen, lineEnd);
  if (new RegExp(AMOUNT_TOKEN_RE.source, "i").test(ownLineRest) || new RegExp(LOOSE_NUMBER_RE.source).test(ownLineRest)) {
    return text.slice(idx, lineEnd);
  }
  let searchFrom = lineEnd + 1;
  for (let i = 0; i < MAX_VALUE_LOOKAHEAD_LINES; i++) {
    const nextEnd = text.indexOf("\n", searchFrom);
    const line = nextEnd !== -1 ? text.slice(searchFrom, nextEnd) : text.slice(searchFrom);
    if (line.trim()) return line;
    if (nextEnd === -1) break;
    searchFrom = nextEnd + 1;
  }
  return "";
}

function editDistanceLe(a, b, maxDist) {
  if (Math.abs(a.length - b.length) > maxDist) return false;
  let prev = Array.from({ length: b.length + 1 }, (_, j) => j);
  for (let i = 1; i <= a.length; i++) {
    const cur = [i];
    for (let j = 1; j <= b.length; j++) {
      const cost = a[i - 1] === b[j - 1] ? 0 : 1;
      cur[j] = Math.min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + cost);
    }
    prev = cur;
  }
  return prev[prev.length - 1] <= maxDist;
}

function findFuzzyLabelledAmount(text) {
  const wordRe = /[A-Za-z]{3,}/g;
  let m;
  while ((m = wordRe.exec(text)) !== null) {
    const word = m[0].toLowerCase();
    for (const label of FUZZY_AMOUNT_LABELS) {
      if (word === label || !editDistanceLe(word, label, 1)) continue;
      const window = labelValueWindow(text, m.index, m[0].length);
      const tokens = matchAllTokens(new RegExp(AMOUNT_TOKEN_RE.source, "gi"), window);
      if (tokens.length) {
        const amt = parseAmount(tokens[0]);
        if (amt !== null && amt >= 10) return amt;
      }
    }
  }
  return null;
}

// ---------- Multi-transaction statement detection ----------
//
// Confirmed real failure (reports/receipt-extraction-baseline-20261013):
// a credit-card statement's "Total Amount due" (the card's outstanding
// BALANCE, not any one transaction's amount) and a FASTag toll statement's
// running balance were both confidently read by the existing amount-label
// priority search as if one of them were a single bill's payable total -
// "total amount due" literally matches the highest-priority "total amount"
// label pattern. This never relies on "the page has many dates" ALONE
// (explicitly required - see the two-path design below): a genuine real
// counter-example already exists in this same corpus (the Home Town
// Resort hotel invoice, ER-40706.pdf pages 10-11, 8+ dated daily room-
// charge lines rolling up to ONE Net Amount total) - it has none of this
// statement vocabulary and must never be suppressed, and isn't, because
// neither path below fires for it.
//
// Path 1 - a statement-balance phrase PAIR. Each pair's two halves
// essentially only co-occur on a credit-card-style statement (an ordinary
// invoice never prints both "Minimum Amount due" and "Total Amount due",
// or both "Opening Balance" and "Closing Balance") - a strong signal on
// its own, no date count needed. This is what makes a short statement
// excerpt (few visible transactions on this particular OCR'd page) still
// correctly suppress its own balance line, consistent with "do not
// substitute the statement balance" regardless of how many transaction
// rows happen to be visible.
// Path 2 - a weaker standalone "statement" heading, reinforced by actually
// counting several distinct transaction dates on the page (reusing the
// same technique already proven for the separate bill-GROUPING concern,
// see backend's bill_grouping.py::_looks_like_multi_transaction_statement)
// - required together, never dates alone, so a one-off document that
// merely contains the word "statement" in passing is not enough by itself.
const STATEMENT_BALANCE_PAIRS = [
  [/\bMINIMUM AMOUNT DUE\b/i, /\bTOTAL AMOUNT DUE\b/i],
  [/\bOPENING BALANCE\b/i, /\bCLOSING BALANCE\b/i],
  [/\bPREVIOUS BALANCE\b/i, /\b(?:PAYMENTS?\s*\/\s*CREDITS?|PURCHASES?\s*\/\s*CHARGES?)\b/i],
];
const STATEMENT_HEADING_RE = /\b(CREDIT CARD STATEMENT|E-STATEMENT|BANK STATEMENT|ACCOUNT STATEMENT|STATEMENT SUMMARY|STATEMENT PERIOD|STATEMENT REFERENCE)\b/i;
const MIN_DISTINCT_DATES_FOR_STATEMENT = 5;

export function detectMultiTransactionStatement(text) {
  const pair = STATEMENT_BALANCE_PAIRS.find(([a, b]) => a.test(text) && b.test(text));
  if (pair) {
    return { isStatement: true, reason: `statement-balance phrase pair found (/${pair[0].source}/ + /${pair[1].source}/)` };
  }
  if (STATEMENT_HEADING_RE.test(text)) {
    const distinctDates = new Set(findAllDates(text).map((d) => d.iso)).size;
    if (distinctDates >= MIN_DISTINCT_DATES_FOR_STATEMENT) {
      return { isStatement: true, reason: `statement heading + ${distinctDates} distinct transaction dates (>= ${MIN_DISTINCT_DATES_FOR_STATEMENT})` };
    }
  }
  return { isStatement: false, reason: null };
}

function findAmount(text) {
  const warnings = [];
  const labeled = new Map();
  let looseUsed = false;

  for (const [priority, , pattern] of AMOUNT_LABEL_PATTERNS) {
    pattern.lastIndex = 0;
    let m;
    while ((m = pattern.exec(text)) !== null) {
      const idx = m.index;
      const window = labelValueWindow(text, idx, m[0].length);
      const tokens = matchAllTokens(new RegExp(AMOUNT_TOKEN_RE.source, "gi"), window);
      if (tokens.length) {
        for (const tok of tokens) {
          const amt = parseAmount(tok);
          if (amt !== null) {
            if (!labeled.has(priority)) labeled.set(priority, new Set());
            labeled.get(priority).add(amt);
          }
        }
      } else {
        const looseTokens = matchAllTokens(new RegExp(LOOSE_NUMBER_RE.source, "g"), window);
        if (looseTokens.length) {
          const amt = parseAmount(looseTokens[0]);
          if (amt !== null && amt >= 10) {
            if (!labeled.has(priority)) labeled.set(priority, new Set());
            labeled.get(priority).add(amt);
            looseUsed = true;
          }
        }
      }
      if (m[0] === "") pattern.lastIndex++;
    }
  }

  if (labeled.size) {
    const bestPriority = Math.max(...labeled.keys());
    const candidates = labeled.get(bestPriority);
    if (candidates.size === 1) {
      const confidence = looseUsed ? "MEDIUM" : "HIGH";
      if (looseUsed) warnings.push("Amount read via a loose fallback pattern - OCR likely obscured the currency/decimal marker.");
      return { amount: [...candidates][0], confidence, warnings };
    }
    warnings.push(`Multiple differing amounts found at the same label priority: ${[...candidates].sort((a, b) => a - b)}.`);
    return { amount: null, confidence: "LOW", warnings };
  }

  const fuzzyAmt = findFuzzyLabelledAmount(text);
  if (fuzzyAmt !== null) {
    warnings.push("Amount label itself was OCR-corrupted (single character) but recovered via fuzzy label matching.");
    return { amount: fuzzyAmt, confidence: "MEDIUM", warnings };
  }

  const allTokens = matchAllTokens(new RegExp(AMOUNT_TOKEN_RE.source, "gi"), text);
  const amounts = allTokens.map(parseAmount).filter((a) => a !== null && a >= 10);
  const distinct = [...new Set(amounts)].sort((a, b) => a - b);
  if (distinct.length === 1) return { amount: distinct[0], confidence: "MEDIUM", warnings };
  if (distinct.length > 1) {
    const best = Math.max(...distinct);
    const others = amounts.filter((a) => a !== best);
    const sumOthers = others.reduce((s, a) => s + a, 0);
    if (others.length && Math.abs(sumOthers - best) < 0.02) {
      warnings.push(`Amount confirmed via arithmetic: components ${[...new Set(others)].sort((a, b) => a - b)} sum to ${best}, which is used as the bill total.`);
      return { amount: best, confidence: "MEDIUM", warnings };
    }
    warnings.push(`Multiple unlabeled amounts found; using the largest (${best}) as a low-confidence candidate total. Other amounts on the page: ${[...new Set(others)].sort((a, b) => a - b)}.`);
    return { amount: best, confidence: "LOW", warnings };
  }
  return { amount: null, confidence: "LOW", warnings };
}

function findDate(text) {
  const lines = text.split("\n");
  let best = null;
  for (let i = 0; i < lines.length; i++) {
    const lower = lines[i].toLowerCase();
    if (DATE_EXCLUDE_LABELS.some((excluded) => lower.includes(excluded))) continue;
    const matchingPriorities = DATE_LABELS.filter(([, label]) => lower.includes(label)).map(([p]) => p);
    if (!matchingPriorities.length) continue;
    const priority = Math.max(...matchingPriorities);
    let dates = findAllDates(lines[i]);
    if (!dates.length && i + 1 < lines.length) dates = findAllDates(lines[i + 1]);
    if (dates.length && (best === null || priority > best.priority)) best = { priority, date: dates[0] };
  }
  if (best !== null) return { date: best.date, confidence: "HIGH", warnings: [] };

  const allDates = findAllDates(text);
  const distinctKeys = [...new Set(allDates.map((d) => d.iso))];
  if (distinctKeys.length === 1) return { date: allDates.find((d) => d.iso === distinctKeys[0]), confidence: "MEDIUM", warnings: [] };
  if (distinctKeys.length > 1) return { date: null, confidence: "LOW", warnings: [`Multiple differing dates found with no clear bill-date label: ${distinctKeys}.`] };
  return { date: null, confidence: "LOW", warnings: [] };
}

function findBillNumber(text) {
  const lines = text.split("\n");
  for (let i = 0; i < lines.length; i++) {
    const lower = lines[i].toLowerCase();
    for (const label of BILL_NUMBER_LABELS) {
      if (lower.includes(label)) {
        const after = lines[i].split(":");
        let candidate = after.length > 1 ? after.slice(1).join(":").trim() : "";
        if (!candidate && i + 1 < lines.length) candidate = lines[i + 1].trim();
        candidate = candidate.replace(/[^A-Za-z0-9\-/]/g, "");
        if (candidate.length >= 3 && candidate.length <= 30) return candidate;
      }
    }
  }
  return null;
}

// ---------- Generic non-vendor document scaffolding ----------
//
// Confirmed real failure (reports/receipt-extraction-baseline-20261013,
// 5 of 13 human-verified samples): the vendor fallback picked a bare UI
// field label ("Name", "Paid to" with the real payee on a DIFFERENT line),
// a payment-status heading ("Transaction Successful"), a document-type
// heading ("TAX INVOICE"), a customer-side metadata caption ("Party's
// GSTIN : ..." - naming the GUEST, not the hotel), and card-slip
// disclaimer boilerplate ("I AM SATISFIED WITH GOOD/SERVICE RECEIVED...") -
// several with decent OCR confidence, so this was never primarily an OCR-
// quality problem.
//
// This is a closed, principled set of generic DOCUMENT-STRUCTURE phrases
// (what role a line plays: a label, a status banner, a heading, a
// disclaimer, a payment-rail name) - never a vendor whitelist and never a
// rejection of an unusual/unfamiliar name. Each pattern targets the
// line's ROLE, not its content's familiarity, so a short, all-caps, or
// otherwise unusual genuine vendor name is never excluded by this list -
// confirmed by this file's own regression tests (test-vendor-extraction-
// fix.mjs cases 2/3/4/7).
//
// A line is excluded only when it is (almost entirely) one of these
// phrases, not merely because it CONTAINS a related word in passing (e.g.
// a genuine vendor line that happens to mention "invoice" elsewhere in a
// longer sentence is not excluded - only a line that IS a bare heading/
// label, give or take a few characters of OCR noise, is).
const GENERIC_NONVENDOR_LINE_RE = [
  // Bare field labels - the line IS just the label, nothing else. A label
  // immediately followed by its own value on the SAME line (e.g. "Guest
  // Name: Mr. X") is a different, longer line and is not matched here.
  /^(name|date|amount|total|merchant|vendor|address|customer\s*details?|order\s*id|ride\s*id|card\s*no\.?|bank\s*(id|mid))\s*[:\-]?\s*$/i,
  // Payment/transaction status banners and generic section headings,
  // tolerating a couple of leading OCR-noise characters (confirmed real
  // case: "q Transaction Successful").
  /^.{0,2}\b(transaction|payment|transfer)\s+(successful|details?|summary)\s*$/i,
  /^.{0,2}\b(order|bill|booking|ride)\s+(summary|details?|history)\s*$/i,
  // Document-type headings, tolerating a few stray leading/trailing OCR
  // noise characters (confirmed real case: "i TAX INVOICE").
  /^.{0,3}\b(tax\s+invoice|retail\s+invoice|cash\s+(bill|memo|invoice)|invoice|receipt|bill|voucher)\b.{0,3}$/i,
  // A bare "Paid to"/"Paid at"/"Paid by"/"Sent to" with nothing
  // recognizable after it on the same line - the real payee/vendor, when
  // present at all, is on a different line in that case.
  /^(paid\s+to|paid\s+at|paid\s+by|sent\s+to)\s*[:\-]?\s*$/i,
  // Customer/guest-side metadata captions - deliberately narrow (requires
  // the "party's"/"customer"/"guest" prefix, tolerating a couple of leading
  // OCR-noise characters - confirmed real case: "! Party's GSTIN : ...")
  // so the VENDOR's own GSTIN/address/name line, which carries no such
  // prefix, is never excluded.
  /^.{0,2}\b(party'?s|customer'?s?|guest)\s+(gstin|name|address|details?)\b/i,
  // Card-terminal / payment-slip disclaimer boilerplate. "issu?[a-z]r
  // agreement" tolerates the common OCR misread "ISSLER" for "ISSUER"
  // (confirmed real case) while staying specific to this standard card-
  // receipt phrase, rather than matching "agreement" bare.
  /\b(agree\s+to\s+pay|signature\s+not\s+required|pin\s+verified|satisfied\s+with\s+(good|service)|customer\s+copy|merchant\s+copy|issu?[a-z]r\s+agreement)\b/i,
  // A bare timestamp line ("02:07 PM on 10 Jul 2026") - common immediately
  // after a payment-status banner on these screenshots, confirmed real
  // case once the banner itself is excluded.
  /^\d{1,2}:\d{2}\s*(am|pm)?\s+on\s+\d{1,2}\s+\w+\s+\d{4}\s*$/i,
  // A scanner-app footer watermark (e.g. "Scanned with OKEN Scanner") -
  // confirmed on multiple real photographed-receipt pages in this corpus.
  /\bscanned\s+with\b/i,
  // Payment-rail / processor names - never the merchant itself.
  /^powered\s+by\b/i,
  /^(phonepe|paytm|google\s*pay|g\s*pay|bhim|upi)\b.{0,20}$/i,
];

function isGenericNonVendorLine(s) {
  return GENERIC_NONVENDOR_LINE_RE.some((re) => re.test(s));
}

export function hasConfidentVendorLine(text) {
  for (const line of text.split("\n")) {
    const s = line.trim();
    if (s.length < 3 || s.length > 80) continue;
    if (isGenericNonVendorLine(s)) continue;
    const lastWords = s.split(/\s+/).slice(-4).join(" ");
    if (VENDOR_SUFFIX_RE.test(lastWords)) return true;
  }
  return false;
}

function findVendor(text) {
  // Deliberately first-match-wins for the strong pass, same as before this
  // fix - a "collect every strong match and abstain on conflict" version
  // was tried and reverted: VENDOR_SUFFIX_RE's suffix-word list (e.g.
  // HARDWARE\w*, SERVICES?) is broad enough to also match an ordinary
  // description/category line ("Hardware Repair Invoice" legitimately
  // contains "Hardware"), not only a genuine company-name ending - so a
  // ordinary single-vendor bill with an unrelated description line further
  // down could spuriously look "conflicting" and lose a vendor it
  // correctly used to find. Tightening VENDOR_SUFFIX_RE itself to avoid
  // that would be a larger, separately-scoped change. True multi-vendor
  // pages are the existing splitBillSegments()'s job, upstream of this
  // function, not something findVendor re-detects on its own.
  for (const line of text.split("\n")) {
    const s = line.trim();
    if (s.length < 3 || s.length > 80) continue;
    if (isGenericNonVendorLine(s)) continue;
    const lastWords = s.split(/\s+/).slice(-4).join(" ");
    if (VENDOR_SUFFIX_RE.test(lastWords)) return { vendor: s, strong: true };
  }

  // No strong company-suffix match anywhere - before falling back to the
  // weaker positional heuristic, check for an objective sign of genuine
  // ambiguity: 2+ DISTINCT GSTINs named in the text (ordinarily each GSTIN
  // belongs to a different registered business) with no strong match to
  // confidently pick one. This is deliberately narrow - it only fires when
  // the strong pass already found nothing, so it never touches the (far
  // more common) case of a single clear vendor whose own GSTIN happens to
  // sit near unrelated reference numbers; multi-GSTIN pages with a clear
  // strong-match vendor (e.g. two distinct "... LTD" names) are already
  // handled by the strong pass above and by splitBillSegments() upstream,
  // not by this check.
  GSTIN_RE.lastIndex = 0;
  const distinctGstins = new Set([...text.toUpperCase().matchAll(new RegExp(GSTIN_RE.source, "g"))].map((m) => m[0]));
  if (distinctGstins.size >= 2) {
    return { vendor: null, strong: false, conflicting: [...distinctGstins] };
  }

  for (const line of text.split("\n")) {
    const s = line.trim();
    if (
      s.length >= 3 &&
      s.length <= 60 &&
      /[A-Za-z]{3,}/.test(s) &&
      !new RegExp(`^(?:${AMOUNT_TOKEN_RE.source})$`, "i").test(s) &&
      !CONTINUATION_MARKER_RE.test(s) &&
      !isGenericNonVendorLine(s)
    ) {
      return { vendor: s, strong: false };
    }
  }
  return { vendor: null, strong: false };
}

function findCategoryFuzzy(text) {
  const wordRe = /[A-Za-z]{4,}/g;
  let m;
  while ((m = wordRe.exec(text)) !== null) {
    const word = m[0].toUpperCase();
    for (const [keyword, label] of FUZZY_CATEGORY_WORDS) {
      if (editDistanceLe(word, keyword, 1)) return { category: label, evidence: m[0] };
    }
  }
  return { category: null, evidence: null };
}

function findCategory(text) {
  for (const [pattern, label] of CATEGORY_KEYWORDS) {
    const re = new RegExp(pattern.source, pattern.flags.includes("g") ? pattern.flags : pattern.flags + "");
    const m = re.exec(text);
    if (m) return { category: label, evidence: m[0] };
  }
  return findCategoryFuzzy(text);
}

export function parseBillText(text) {
  const bill = {
    vendor: null,
    billNumber: null,
    billDate: null,
    amount: null,
    gstin: null,
    description: null,
    categoryGuess: null,
    rawText: text.slice(0, 2000),
    confidence: "LOW",
    status: "REVIEW_REQUIRED",
    warnings: [],
    // Additive field (see detectMultiTransactionStatement's module
    // comment) - false for every pre-existing bill, since nothing before
    // this check could ever set it true. Carries WHY amount was
    // suppressed, alongside the existing warnings array, rather than
    // changing what `amount: null` on its own means for every other
    // caller already checking that field.
    isMultiTransactionStatement: false,
  };

  const statementCheck = detectMultiTransactionStatement(text);
  bill.isMultiTransactionStatement = statementCheck.isStatement;

  let amount, amountConfidence, amountWarnings;
  if (statementCheck.isStatement) {
    // Never substitutes the statement/running balance for an individual
    // transaction amount - bill_parser's single-page, no-external-context
    // view has no way to tell WHICH of a statement's many transaction rows
    // is the one relevant to a specific expense claim, so the individual
    // amount is left genuinely unresolved rather than guessed from the
    // statement's own totals.
    amount = null;
    amountConfidence = "LOW";
    amountWarnings = [
      `This page looks like a multi-transaction statement, not a single bill (${statementCheck.reason}) - ` +
        "the individual transaction amount can't be reliably identified from the statement's own balance/total figures, so amount is left unresolved rather than using the statement balance.",
    ];
  } else {
    ({ amount, confidence: amountConfidence, warnings: amountWarnings } = findAmount(text));
  }
  bill.amount = amount;
  bill.warnings.push(...amountWarnings);

  const { date: billDate, confidence: dateConfidence, warnings: dateWarnings } = findDate(text);
  bill.billDate = billDate;
  bill.warnings.push(...dateWarnings);

  GSTIN_RE.lastIndex = 0;
  const gstinMatch = GSTIN_RE.exec(text.toUpperCase());
  bill.gstin = gstinMatch ? gstinMatch[0] : null;

  bill.billNumber = findBillNumber(text);

  const { vendor, strong: vendorStrong, conflicting } = findVendor(text);
  bill.vendor = vendor;
  if (conflicting) {
    bill.warnings.push(`Multiple distinct GSTINs found with no single strongly-matched vendor (${conflicting.join(" / ")}); vendor left unresolved rather than guessing.`);
  }

  const { category: categoryGuess, evidence } = findCategory(text);
  bill.categoryGuess = categoryGuess;
  bill.description = evidence;

  // NVIDIA supplementary-fill pass intentionally omitted - see module
  // header comment; this mirrors the real app's own default (no-op)
  // behavior when SPINCO_NVIDIA_API_KEY isn't configured.

  if (bill.amount === null) bill.warnings.push("Bill amount could not be reliably determined.");
  if (bill.billDate === null) bill.warnings.push("Bill date could not be reliably determined.");
  if (bill.vendor === null) bill.warnings.push("Vendor could not be reliably determined.");

  const strongSignals = [
    bill.amount !== null && amountConfidence === "HIGH",
    bill.billDate !== null && dateConfidence === "HIGH",
    Boolean(bill.gstin),
    vendorStrong,
  ].filter(Boolean).length;

  if (bill.amount !== null && bill.billDate !== null && strongSignals >= 2) {
    bill.confidence = "HIGH";
    bill.status = "EXTRACTED";
  } else if (bill.amount !== null) {
    bill.confidence = amountConfidence !== "LOW" ? "MEDIUM" : "LOW";
    bill.status = amountConfidence !== "LOW" ? "EXTRACTED" : "REVIEW_REQUIRED";
  } else {
    bill.confidence = "LOW";
    bill.status = "REVIEW_REQUIRED";
  }

  return bill;
}

export function splitBillSegments(text) {
  const upper = text.toUpperCase();
  GSTIN_RE.lastIndex = 0;
  const matches = [...upper.matchAll(new RegExp(GSTIN_RE.source, "g"))];
  const distinctGstins = new Set(matches.map((m) => m[0]));
  if (distinctGstins.size < 2) return [text];

  const positions = matches.map((m) => m.index).sort((a, b) => a - b);
  const boundaries = [0];
  for (let i = 1; i < positions.length; i++) {
    boundaries.push(Math.floor((positions[i - 1] + positions[i]) / 2));
  }
  boundaries.push(text.length);

  const segments = [];
  for (let i = 0; i < boundaries.length - 1; i++) {
    const segment = text.slice(boundaries[i], boundaries[i + 1]).trim();
    if (segment) segments.push(segment);
  }
  return segments.length ? segments : [text];
}
