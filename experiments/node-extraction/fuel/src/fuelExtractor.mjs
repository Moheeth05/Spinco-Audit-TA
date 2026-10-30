// EXPERIMENTAL, evidence-aware extractor for exactly three fields of an Indian fuel-pump receipt:
//   pump_name, transaction_date (YYYY-MM-DD), total_amount.
// Deterministic rules over OCR text + line geometry. NOT a trained model; nothing here was trained or fine-tuned.
// Local only: no network, no hosted AI. Not connected to the production backend.
//
// Principles (from the task brief):
//   * Never invent a value. Every returned value carries the printed raw text and the OCR line(s) it came from.
//   * Never calculate a replacement amount (no rate x quantity); rate/quantity are never extracted - they are only
//     recognised BY LABEL so their numbers are not mistaken for the total.
//   * Day/month order is resolved only from evidence in the receipt; otherwise the value stays null, both readings are
//     kept in `ambiguity`, and review is required. The claim date is never an input.
//   * Missing / ambiguous / conflicting evidence => review_required (never a silent best guess).
//   * `confidence` is a HEURISTIC score, labelled as such (confidence_type), not a calibrated probability.

export const VERSION = "fuel-extractor/0.1-experimental";

// ----------------------------------------------------------------------------------------------- text helpers
const MONTHS = { jan: 1, feb: 2, mar: 3, apr: 4, may: 5, jun: 6, jul: 7, aug: 8, sep: 9, sept: 9, oct: 10, nov: 11, dec: 12 };
const MONTH_FULL = { january: 1, february: 2, march: 3, april: 4, june: 6, july: 7, august: 8, september: 9, october: 10, november: 11, december: 12 };
const MONTH_RE = "(?:JAN(?:UARY)?|FEB(?:RUARY)?|MAR(?:CH)?|APR(?:IL)?|MAY|JUNE?|JULY?|AUG(?:UST)?|SEP(?:T(?:EMBER)?)?|OCT(?:OBER)?|NOV(?:EMBER)?|DEC(?:EMBER)?)";
const monthNum = (s) => {
  const k = s.toLowerCase();
  return MONTHS[k.slice(0, 3)] && (MONTHS[k] || MONTH_FULL[k] || MONTHS[k.slice(0, 3)]);
};

function normalizeUnicode(s) {
  return (s || "")
    .replace(/[\u00a0\u2000-\u200a\u202f\u205f\u3000]/g, " ")
    .replace(/[\u200b-\u200d\ufeff\u00ad]/g, "")
    .replace(/[\u2010-\u2015\u2212]/g, "-");
}

const clean = (s) => normalizeUnicode(s).replace(/\s+/g, " ").trim();

function buildLines(ocr) {
  const src = ocr.lines && ocr.lines.length ? ocr.lines : (ocr.text || "").split("\n").filter((t) => t.trim()).map((t, i) => ({ text: t, bbox: { x0: 0, y0: i * 20, x1: 100, y1: i * 20 + 18 }, confidence: ocr.confidence ?? null, words: [] }));
  return src
    .map((l, i) => ({ i, raw: l.text, text: clean(l.text), bbox: l.bbox, conf: l.confidence, words: l.words || [], edge: l.edge || null }))
    .filter((l) => l.text.length > 0);
}

const lineEvidence = (l, role) => ({ role, line_index: l.i, text: l.text, ocr_line_confidence: l.conf, bbox: l.bbox });

// ----------------------------------------------------------------------------------------------- fuel-receipt evidence
const FUEL_WORDS = /\b(PETROL|DIESEL|FUELS?|AUTO\s*FUELS?|NOZZLE|NOZL|DENSITY|PRESET|XP\s*95|POWER|SPEED|CNG|LPG|MS|HSD|HP\s*DEALER|OUTLET|FILLING|PETROLEUM|PETRO)\b/i;
const FUEL_LABELS = /\b(DENSITY|NOZZLE|NOZL|PRESET|RATE\s*\/?\s*\(?\s*(RS\s*\/?\s*)?L(TR)?|VOLUME|VOL\b|FIP|FCC|PRODUCT|FUEL)\b/i;

const BRANDS = [
  { id: "BPCL", name: "Bharat Petroleum", re: /BHARAT\s*PETRO[A-Z]*|\bBPCL\b|\bB\.?P\.?C\.?L\b/i },
  { id: "HPCL", name: "HP", re: /HINDUSTAN\s*PETRO[A-Z]*|\bHPCL\b|\bH\.?P\.?C\.?L\b/i },
  { id: "IOCL", name: "IndianOil", re: /INDIAN\s*OIL|INDIANOIL|\bIOCL\b|\bI\.?O\.?C\b/i },
  { id: "NAYARA", name: "Nayara Energy", re: /NAYARA|ESSAR\s*OIL/i },
  { id: "JIOBP", name: "jio-bp", re: /JIO[\s\-]*BP|\bJIO\b/i },
  { id: "SHELL", name: "Shell", re: /\bSHELL\b/i },
];
// A bare "HP" / "BP" is a brand only when the receipt already shows fuel evidence AND it stands alone as (nearly) the whole line.
const BARE_BRAND = [
  { id: "HPCL", name: "HP", re: /^\s*H\.?P\.?\s*$/i },
  { id: "BPCL", name: "Bharat Petroleum", re: /^\s*B\.?P\.?\s*$/i },
];

// ----------------------------------------------------------------------------------------------- pump name
const BOILERPLATE = /\b(WELCOME[S]?(\s+TO|\s+YOU)?|THANK(S)?\s*YOU|TAX\s*INVOICE|CASH\s*MEMO|CASH\s*BILL|ORIGINAL|DUPLICATE|REPRINT|SALE\s*TRANSACTION|RECORDS?|RECORD\s*NO|RECEIPT|INVOICE|VISIT|AGAIN|PLEASE|CUSTOMER\s*COPY|PAGE|COPY)\b/i;
const ADDRESS = /\b(ROAD|RD|NAGAR|LAYOUT|BLOCK|EXT|EXTN|DIST|DISTRICT|TEL|PH|PHONE|MOB|CELL|GST|GSTIN|TIN|VAT|CST|LST|PIN|NEAR|OPP|NH|HIGHWAY|BYPASS|BY\s*PASS|SECTOR|PLOT|D\.?NO|S\.?NO|NO\.)\b/i;
const DEALER_CUES = /\b(FUELS?|AUTO\s*FUELS?|AUTOFUELS?|PETROLEUM|PETRO|SERVICE|STATION|SSTN|AGENCIES|AGENCY|ENTERPRISES?|FILLING|CENTRE|CENTER|POINT|DEALER\s*OUTLET|PUMP|CARE|MOTORS?|FUEL\s*STATION)\b/i;
const FIELD_LABEL_LINE = /^(DATE|TIME|BILL|INV|INVOICE|TXN|TRNS|RECEIPT|VEH|VEHI|MOB|PRODUCT|FUEL(?!\s+STATION)|NOZ|NOZZLE|FIP|FCC|DENSITY|RATE|AMOUNT|VOLUME|SALE|ATND|LOCAL|PRESET|BAY|PAYMODE|ATTENDANT|TEL|PUMP)\b/i;

const alphaRatio = (s) => (s.replace(/[^A-Za-z]/g, "").length / Math.max(1, s.replace(/\s/g, "").length));

// A logo's wordmark is often OCR'd as separate short lines ("Bharat" / "Petroleum"): these are brand fragments, never a dealer.
const BRAND_FRAGMENT = new Set(["BHARAT", "PETROLEUM", "BHARATPETROLEUM", "INDIAN", "OIL", "INDIANOIL", "HINDUSTAN", "HP", "BP", "ENERGY", "NAYARA", "SHELL", "PETROLEUMCORP", "CORP", "WELCOMES", "YOU", "WELCOME"]);
const isBrandFragment = (t) => BRAND_FRAGMENT.has(t.toUpperCase().replace(/[^A-Z]/g, ""));

// "1RIMPANAM" -> "IRIMPANAM": a digit at the start of an otherwise alphabetic word is an OCR confusion in a NAME (never applied to numbers).
function fixLeadingDigits(t) {
  return t.replace(/\b([01258])([A-Za-z]{4,})\b/g, (m, d, rest) => ({ 0: "O", 1: "I", 2: "Z", 5: "S", 8: "B" }[d] || d) + rest);
}
// Remove OCR debris at the ends of a header line: tokens with no letters ("7", "|", ">", "-").
function stripJunkEdges(t) {
  const toks = t.split(/\s+/).filter(Boolean);
  while (toks.length && !/[A-Za-z]/.test(toks[toks.length - 1])) toks.pop();
  while (toks.length && !/[A-Za-z]/.test(toks[0])) toks.shift();
  return toks.join(" ").replace(/[|>\]\[;:,\-]+$/g, "").trim();
}
// Is this line plausibly a printed name rather than OCR noise?
function plausibleName(t, conf) {
  const letters = t.replace(/[^A-Za-z]/g, "").length;
  if (letters < 6) return false;
  const toks = t.split(/\s+/);
  if (toks.some((w) => /[a-z][A-Z]{2,}/.test(w))) return false; // mixed-case junk such as "coosatiAMAGRRRARRR"
  const symbols = (t.match(/[^A-Za-z0-9 .&'\/\-]/g) || []).length;
  if (symbols / t.length > 0.12) return false;
  if (!toks.some((w) => w.replace(/[^A-Za-z]/g, "").length >= 4 && /[AEIOUYaeiouy]/.test(w))) return false;
  if (typeof conf === "number" && conf < 0.25) return false;
  return true;
}

// Drop leading/trailing words the OCR itself was unsure about ("ESS _ GAURI SHIVAAY PETROLEUM (HB." -> the middle). Keeps at least 2 words.
function trimWeakEdgeWords(line) {
  const w = (line.words || []).filter((x) => x.text && x.text.trim());
  if (w.length < 3) return { text: line.text, trimmed: false, words: w };
  let a = 0, b = w.length;
  const weak = (x) => (typeof x.confidence === "number" && x.confidence < 0.5) || !/[A-Za-z]{3,}/.test(x.text);
  while (a < b - 2 && weak(w[a])) a++;
  while (b - 2 > a && weak(w[b - 1])) b--;
  const kept = w.slice(a, b);
  return { text: clean(kept.map((x) => x.text).join(" ")), trimmed: a > 0 || b < w.length, words: kept };
}
// The least-confident real word (>= 3 letters) of a header line: OCR line confidence hides one badly read word.
function minWordConfidence(words, fallback) {
  const c = (words || []).filter((x) => /[A-Za-z]{3,}/.test(x.text) && typeof x.confidence === "number").map((x) => x.confidence);
  return c.length ? Math.min(...c) : fallback ?? null;
}
const PUMP_MIN_WORD_CONF = 0.8; // heuristic gate chosen on DEVELOPMENT data only; not a calibrated probability

// A greeting line ("Welcomes You", "Thank you visit again") possibly OCR-garbled ("Kelcones You"): never a name.
function isGreetingLine(t) {
  const toks = t.toUpperCase().replace(/[^A-Z ]/g, " ").split(/\s+/).filter(Boolean);
  if (!toks.length) return false;
  const greet = toks.filter((w) => ["YOU", "TO", "THANK", "THANKS", "VISIT", "AGAIN", "PLEASE", "AT"].includes(w) || (w.length >= 6 && (levenshtein(w, "WELCOMES") <= 2 || levenshtein(w, "WELCOME") <= 2)));
  return greet.length >= Math.ceil(toks.length * 0.6) && greet.length > 0;
}

// Tolerate ONE OCR character error in a brand word ("NAVARA" -> NAYARA, "BHARAT" -> "BHARAI"). Only words of 5+ letters; returns the corrected text.
const BRAND_WORDS = ["BHARAT", "HINDUSTAN", "NAYARA", "SHELL", "INDIANOIL"];
function canonBrandText(text) {
  return text.replace(/[A-Za-z]{5,}/g, (w) => {
    const u = w.toUpperCase();
    for (const b of BRAND_WORDS) if (u !== b && Math.abs(u.length - b.length) <= 1 && levenshtein(u, b) <= 1) return b;
    return w;
  });
}

// A line that STARTS with an oil-company name and otherwise holds only generic company/outlet words ("Shell Select Station", "Shell Retail India", "jio-bp mobility",
// "Nayara Energy Limited") is the brand's own heading, not a dealer. Dealer names that merely contain a brand word after other words are not affected.
const GENERIC_BRAND_WORDS = new Set(["SELECT", "STATION", "STATIONS", "RETAIL", "RETAILS", "INDIA", "MOBILITY", "PULSE", "ENERGY", "LIMITED", "LTD", "PVT", "PRIVATE", "CORP", "CORPORATION", "COMPANY", "XTRAREWARDS", "MARKETS", "MARKETING", "AND", "THE", "OF", "PETROLEUM", "OIL", "FUEL"]);
function isBrandHeading(text) {
  const t = canonBrandText(text).replace(/[^A-Za-z ]/g, " ").replace(/\s+/g, " ").trim().toUpperCase();
  const m = t.match(/^(SHELL|NAYARA|JIO ?BP|BHARAT PETROLEUM|BHARAT|INDIAN ?OIL|HINDUSTAN PETROLEUM|HINDUSTAN)\b\s*(.*)$/);
  if (!m) return false;
  const rest = m[2].split(" ").filter(Boolean);
  return rest.every((w) => GENERIC_BRAND_WORDS.has(w));
}

function isBrandOnlyLine(text) {
  const t = canonBrandText(text).replace(/[^A-Za-z ]/g, " ").replace(/\s+/g, " ").trim();
  if (!t) return false;
  const rest = BRANDS.reduce((acc, b) => acc.replace(new RegExp(b.re.source, "gi"), " "), t).replace(/\b(CORP|CORPORATION|LIMITED|LTD|DEALER|ENERGY|WELCOME|TO|THE|OF|INDIA|PVT|COMPANY)\b/gi, " ").replace(/\s+/g, " ").trim();
  return rest.length <= 2;
}

function detectBrand(lines, hasFuelEvidence) {
  const hits = [];
  for (const l of lines) {
    const canon = canonBrandText(l.text);
    for (const b of BRANDS) if (b.re.test(l.text) || b.re.test(canon)) hits.push({ brand: b, line: l, kind: "name" });
  }
  if (hasFuelEvidence) {
    for (const l of lines.slice(0, 14)) for (const b of BARE_BRAND) if (b.re.test(l.text)) hits.push({ brand: b, line: l, kind: "bare_wordmark" });
  }
  if (!hits.length) return null;
  // prefer an early, name-based hit
  hits.sort((a, b) => (a.kind === "name" ? 0 : 1) - (b.kind === "name" ? 0 : 1) || a.line.i - b.line.i);
  const distinct = new Set(hits.filter((h) => h.kind === "name").map((h) => h.brand.id));
  return { ...hits[0], conflict: distinct.size > 1 ? [...distinct] : null };
}

function pumpName(lines, hasFuelEvidence, warnings) {
  const brandHit = detectBrand(lines, hasFuelEvidence);
  // header region: lines before the first field-label line (or the first 12 lines)
  let end = lines.findIndex((l) => FIELD_LABEL_LINE.test(l.text) && !BOILERPLATE.test(l.text));
  if (end < 0 || end > 12) end = Math.min(lines.length, 12);
  const header = lines.slice(0, Math.max(end, 1));
  const candidates = [];
  header.forEach((l, k) => {
    const trim = trimWeakEdgeWords(l);
    // a printed label in front of the name ("Dealer: M/S ...", "Station : ...") is a cue and is never part of the name
    let labelled = false;
    let t = trim.text.replace(/^\s*(?:(?:[A-Za-z]+\s+){0,2}(?:DEALER|STATION|OUTLET|RETAILER|PUMP|BUNK|SITE|NAME)(?:\s+NAME)?\s*:|(?:DEALER|STATION|OUTLET|RETAILER|PUMP|NAME)(?:\s+NAME)?\s+-)\s*/i, () => ((labelled = true), "")); // a label ends in a colon ("Dealer:", "Retail Outlet :", "Fuel Station:"); a DASH counts only after a bare label word ("Station Name - X") so that "KAVERI SERVICE STATION - Coimbatore" is a name, not a label
    t = t.replace(/^\s*(WELCOME(S)?(\s+TO)?|M\s*\/\s*S\.?)\s*/i, "").trim();
    const nextIsDealerRole = Boolean(header[k + 1] && /\b(BPCL|HPCL|IOCL|BP|HP|IOC)\s+DEALER\b/i.test(header[k + 1].text));
    t = t.replace(/\b(BPCL|HPCL|IOCL)\s+DEALER\b/i, "").trim();
    t = stripJunkEdges(fixLeadingDigits(t));
    // "<NAME> - <CITY>": keep the part that looks like the dealer name
    const dash = t.split(/\s+[-–]\s+/);
    if (dash.length === 2 && DEALER_CUES.test(dash[0]) && !DEALER_CUES.test(dash[1]) && dash[0].replace(/[^A-Za-z]/g, "").length >= 6) t = dash[0].trim();
    if (isGreetingLine(t)) return;
    const brandPrefixed = /^(BP|HP|IOC|IOCL|BPCL|HPCL)\s+[A-Za-z]{4,}/i.test(t);
    // a line carrying several digits is a date/amount/id line, never a name (checked on the ORIGINAL text, before trimming)
    if ((l.text.match(/\d/g) || []).length >= 4) return;
    if (t.length < 3 || BOILERPLATE.test(t) && alphaRatio(t) > 0.9 && !DEALER_CUES.test(t)) return;
    if (/\d{5,}/.test(t) || ADDRESS.test(t) || /\d/.test(t) && alphaRatio(t) < 0.8) return;
    if (alphaRatio(t) < 0.75) return;
    if (isBrandOnlyLine(t) || isBrandFragment(t) || isBrandHeading(t)) return;
    if (!plausibleName(t, l.conf)) return;
    const letters = t.replace(/[^A-Za-z]/g, "");
    let score = 0;
    score += Math.max(0, 6 - k); // earlier is better
    const cue = DEALER_CUES.test(t) || nextIsDealerRole || brandPrefixed || labelled;
    if (cue) score += 5;
    if (t === t.toUpperCase()) score += 1;
    if (letters.length >= 6) score += 1;
    if (/\bDEALER\b/i.test(l.text) && !DEALER_CUES.test(t)) score -= 1;
    candidates.push({ text: t, line: l, score, k, cue, trimmed: trim.trimmed, minWord: minWordConfidence(trim.words, l.conf) });
  });
  candidates.sort((a, b) => b.score - a.score || a.k - b.k);
  // the highest scoring candidate that is not merely a place name following a better candidate
  let best = candidates[0] || null;
  const warn = [];
  if (best && candidates.length > 1 && candidates[1].score === best.score && candidates[1].k !== best.k && !DEALER_CUES.test(best.text) && !DEALER_CUES.test(candidates[1].text)) {
    warn.push("Several equally plausible header lines; dealer/station name unclear.");
  }
  const evidence = [];
  let raw = null;
  let normalized = null;
  let review = true;
  let ambiguity = null;
  let basis = null;
  const meanConf = (ls) => {
    const c = ls.map((x) => x.conf).filter((x) => typeof x === "number");
    return c.length ? c.reduce((a, b) => a + b, 0) / c.length : null;
  };
  // A header line with no dealer-type cue is as likely to be a place name as a dealer. When the brand is known, report the brand
  // (review) and keep the line only as raw evidence; never present a possible place name as the station.
  if (best && !best.cue && brandHit) {
    evidence.push(lineEvidence(best.line, "unconfirmed_header_line_not_used_as_name"));
    warn.push(`Header line '${best.text}' has no dealer/station cue and may be a place name; not used as the pump name.`);
    best = null;
  }
  if (best) {
    raw = best.text;
    normalized = best.text;
    evidence.push(lineEvidence(best.line, "dealer_or_station_header_line"));
    basis = best.cue ? "dealer_cue_word" : "header_position_only";
    const conf = best.line.conf;
    review = !(basis === "dealer_cue_word" && (conf === null || conf >= 0.6) && warn.length === 0);
    if (basis === "header_position_only") ambiguity = "No dealer-type word in the chosen header line; it could be a place name.";
    if (conf !== null && conf < 0.6) ambiguity = `Low OCR confidence (${conf.toFixed(2)}) on the chosen header line.`;
    if (best.minWord !== null && best.minWord < PUMP_MIN_WORD_CONF) {
      review = true;
      ambiguity = (ambiguity ? ambiguity + " " : "") + `The least certain word of the name was read with OCR confidence ${best.minWord.toFixed(2)}.`;
    }
    if (best.trimmed) {
      review = true;
      ambiguity = (ambiguity ? ambiguity + " " : "") + "Uncertain leading/trailing words were dropped, so the name may be incomplete.";
    }
    if (best.line.edge && (best.line.edge.left || best.line.edge.right)) {
      review = true;
      ambiguity = (ambiguity ? ambiguity + " " : "") + "The name line touches the edge of the image, so its first or last letters may be cut off.";
    }
  }
  if (brandHit) {
    evidence.push({ ...lineEvidence(brandHit.line, brandHit.kind === "bare_wordmark" ? "brand_wordmark_in_logo_or_header" : "brand_text"), brand: brandHit.brand.id });
    if (brandHit.conflict) {
      warn.push(`More than one oil-company brand appears (${brandHit.conflict.join(", ")}).`);
      review = true;
      ambiguity = (ambiguity ? ambiguity + " " : "") + `Conflicting brands: ${brandHit.conflict.join(", ")}.`;
    }
  }
  if (!best && brandHit) {
    // no dealer line found: the brand is the best-supported candidate, but the station is not identified
    raw = brandHit.line.text;
    normalized = brandHit.brand.name;
    review = true;
    basis = brandHit.kind === "bare_wordmark" ? "brand_wordmark_only" : "brand_text_only";
    ambiguity = "Only the oil-company brand was read; no dealer/station name could be identified.";
  }
  if (!best && !brandHit) {
    warn.push("No pump / dealer name could be read.");
  }
  warnings.push(...warn);
  return {
    raw_value: raw,
    normalized_value: normalized,
    confidence: normalized === null ? null : round2(Math.max(0.2, Math.min(0.95, (best ? best.line.conf ?? 0.6 : brandHit?.line.conf ?? 0.5) * (review ? 0.7 : 1)))),
    confidence_type: "heuristic_uncalibrated",
    evidence,
    ambiguity,
    review_required: review || normalized === null,
    basis,
  };
}

const round2 = (x) => Math.round(x * 100) / 100;

// ----------------------------------------------------------------------------------------------- dates
function safeDate(y, m, d) {
  if (y < 2000 || y > 2099 || m < 1 || m > 12 || d < 1 || d > 31) return null;
  const dt = new Date(Date.UTC(y, m - 1, d));
  if (dt.getUTCFullYear() !== y || dt.getUTCMonth() !== m - 1 || dt.getUTCDate() !== d) return null;
  return `${y}-${String(m).padStart(2, "0")}-${String(d).padStart(2, "0")}`;
}

// Digits that OCR commonly confuses; only ever applied INSIDE a date-shaped token, and the result is flagged as repaired.
const DIGIT_FIX = { O: "0", o: "0", Q: "0", D: "0", I: "1", l: "1", "|": "1", S: "5", B: "8", Z: "2" };

const NUM_DATE = /(?<![\d/.\-])(\d{1,2})([/.\-])(\d{1,2})\2(\d{4}|\d{2})(?!\d)/g;
const ISO_DATE = /(?<![\d/\-])(\d{4})([\-/])(\d{1,2})\2(\d{1,2})(?!\d)/g;
const DMY_NAME = new RegExp(`(?<![\\d/.\\-])(\\d{1,2})(?:ST|ND|RD|TH)?[ \\-/.]*(${MONTH_RE})[ \\-/.,']*(\\d{4}|\\d{2})(?!\\d)(?!:\\d)`, "gi");
const DMY_NAME_GLUED = new RegExp(`(?<![\\d/.\\-])(\\d{1,2})[ \\-/.]*(${MONTH_RE})[ \\-/.,']*(\\d{4})(?=\\d{2}:\\d{2})`, "gi");
const MDY_NAME = new RegExp(`(?<![A-Za-z\\d])(${MONTH_RE})\\.?[ \\-/.]*(\\d{1,2})(?:ST|ND|RD|TH)?[ \\-/.,']+(\\d{4})(?!\\d)`, "gi");
const CUE_MDY = /\b(?:MM|M)\s*[/-]\s*(?:DD|D)\s*[/-]\s*(?:YYYY|YY)\b/i;
const CUE_DMY = /\b(?:DD|D)\s*[/-]\s*(?:MM|M)\s*[/-]\s*(?:YYYY|YY)\b/i;

const TXN_LABEL = /\b(SALE\s*DATE|TRANSACTION\s*DATE|TXN\s*DATE|BILL\s*DATE|INVOICE\s*DATE|INV\s*DATE|DATE\s*OF\s*SALE|DATE|DT|TXST|TXEND|TX\s*ST|TX\s*END)\b/i;
const PRINT_LABEL = /\b(PRINTED(\s*ON)?|PRINT\s*DATE|REPRINT(ED)?|GENERATED|PRINT\s*TIME)\b/i;
const EXCLUDE_DATE_LABEL = /\b(EXPIR\w*|VALID\s*(UNTIL|UPTO|TILL)|DUE\s*DATE|PAYMENT\s*DATE|FCC\s*DATE|DOB|BIRTH)\b/i;

function twoDigitYear(y) {
  return y.length === 2 ? 2000 + Number(y) : Number(y);
}

function scanDates(text, repaired) {
  const out = [];
  const push = (m, kind, value, candidates, extra = {}) => out.push({ raw: m[0].trim(), index: m.index, kind, value, candidates, repaired: Boolean(repaired), ...extra });
  for (const m of text.matchAll(ISO_DATE)) {
    const v = safeDate(+m[1], +m[3], +m[4]);
    if (v) push(m, "iso", v, [v]);
  }
  for (const m of text.matchAll(DMY_NAME_GLUED)) {
    const v = safeDate(+m[3], monthNum(m[2]), +m[1]);
    if (v) push(m, "month_name", v, [v]);
  }
  for (const m of text.matchAll(DMY_NAME)) {
    const v = safeDate(twoDigitYear(m[3]), monthNum(m[2]), +m[1]);
    if (v && !out.some((o) => Math.abs(o.index - m.index) < 3 && o.value === v)) push(m, "month_name", v, [v]);
  }
  for (const m of text.matchAll(MDY_NAME)) {
    const v = safeDate(+m[3], monthNum(m[1]), +m[2]);
    if (v) push(m, "month_name", v, [v]);
  }
  for (const m of text.matchAll(NUM_DATE)) {
    if (m[2] === "." && m[4].length !== 4) continue; // dotted 2-digit year collides with decimals/versions
    const a = +m[1], b = +m[3], y = twoDigitYear(m[4]);
    const dmy = safeDate(y, b, a);
    const mdy = safeDate(y, a, b);
    if (!dmy && !mdy) continue;
    const sig = `${m[2]}|${m[4].length}`;
    if (dmy && mdy && dmy !== mdy) push(m, "numeric_ambiguous", null, [dmy, mdy], { sig, dmy, mdy, a, b });
    else push(m, "numeric", dmy || mdy, [dmy || mdy], { sig, order: dmy && mdy ? "either" : dmy ? "DMY" : "MDY" });
  }
  return out;
}

// RECEIPT-FAMILY-SPECIFIC rule, not a general date rule.
// The pump-automation receipts seen in the authentic corpus (BPCL, HP and IndianOil printouts of the "Physical Receipt" family) print their bill/invoice number
// as "<Mon>-<digits>-ORGNL" ("Bill No: Sep-894177-ORGNL", "INV NO.: Jun-553819-ORGNL"). In every authentic example the month in that number equals the month of the
// printed date (reports/bill_number_month_rule_evidence.md). The rule is therefore limited to that exact signature: a month abbreviation, a number of 3+ digits and the
// literal ORGNL suffix, on a line labelled Bill/INV/Invoice/Receipt. A bare month word anywhere else, or a number without the ORGNL suffix, is NOT used.
// It only ever SELECTS between the day/month readings already present in a printed numeric date; it never supplies a missing day, month or year, and never overrides
// an unambiguous date. If the token's month agrees with neither or with both readings, the date stays unresolved.
const BILL_NUMBER_MONTH_SIGNATURE = new RegExp(`\\b(${MONTH_RE})\\s*-\\s*\\d{3,}\\s*-\\s*ORGNL\\b`, "i");
function monthTokens(lines) {
  const toks = [];
  for (const l of lines) {
    if (!/\b(BILL|INV|INVOICE|RECEIPT)\b/i.test(l.text)) continue;
    const m = l.text.match(BILL_NUMBER_MONTH_SIGNATURE);
    if (m) toks.push({ month: monthNum(m[1]), line: l, text: m[0] });
  }
  return toks;
}

function transactionDate(lines, fullText, warnings) {
  const cueM = CUE_MDY.test(fullText), cueD = CUE_DMY.test(fullText);
  const cue = cueM && !cueD ? "MDY" : cueD && !cueM ? "DMY" : null;
  let found = [];
  for (const l of lines) {
    const prev = lines[lines.indexOf(l) - 1];
    for (const d of scanDates(l.text, false)) found.push({ ...d, line: l, labelText: labelFor(l.text, d.index, prev) });
  }
  let repairedUsed = false;
  if (!found.length) {
    for (const l of lines) {
      const fixed = l.text.replace(/(?<![A-Za-z])[\dOoQDIl|SBZ]{1,2}[/.\-][\dOoQDIl|SBZ]{1,2}[/.\-][\dOoQDIl|SBZ]{2,4}(?![A-Za-z])/g, (t) => t.replace(/[OoQDIl|SBZ]/g, (c) => DIGIT_FIX[c] ?? c));
      if (fixed !== l.text) {
        for (const d of scanDates(fixed, true)) found.push({ ...d, line: l, labelText: labelFor(fixed, d.index, null) });
      }
    }
    repairedUsed = found.length > 0;
  }
  found = found.filter((d) => !EXCLUDE_DATE_LABEL.test(d.labelText));
  if (!found.length) {
    warnings.push("No date could be read.");
    return nullField("transaction_date", "No date text found.");
  }
  const classify = (d) => (PRINT_LABEL.test(d.labelText) ? "print" : TXN_LABEL.test(d.labelText) ? "transaction" : "unlabelled");
  found.forEach((d) => (d.cls = classify(d)));
  const ws = found.filter((d) => d.cls !== "print");
  const printed = found.filter((d) => d.cls === "print");
  const pool = ws.length ? ws : [];
  if (!pool.length) {
    warnings.push("Only a print-date was found; the transaction date is not established.");
    return { ...nullField("transaction_date", "Only a print date was found."), raw_value: printed[0].raw, evidence: printed.map((d) => lineEvidence(d.line, "print_date_candidate")) };
  }
  // order evidence from the document itself
  const proven = new Map(); // sig -> set of orders proven by unambiguous dates
  for (const d of found) if (d.kind === "numeric" && d.order && d.order !== "either") (proven.get(d.sig) || proven.set(d.sig, new Set()).get(d.sig)).add(d.order);
  const months = monthTokens(lines);
  const resolve = (d) => {
    if (d.kind !== "numeric_ambiguous") return { value: d.value, method: d.kind, evidenceText: null, orderOK: true };
    if (cue) return { value: cue === "DMY" ? d.dmy : d.mdy, method: "explicit_format_cue", evidenceText: `The receipt states the format ${cue}.` };
    const s = proven.get(d.sig);
    if (s && s.size === 1) {
      const o = [...s][0];
      return { value: o === "DMY" ? d.dmy : d.mdy, method: "same_receipt_unambiguous_date_same_format", evidenceText: `Another date on this receipt in the same printed format is only valid as ${o}.` };
    }
    const mt = months.filter((t) => t.month === +d.dmy.slice(5, 7) || t.month === +d.mdy.slice(5, 7));
    const matching = months.filter((t) => [d.dmy, d.mdy].filter((c) => +c.slice(5, 7) === t.month).length === 1);
    if (months.length && matching.length === months.length) {
      const pick = [d.dmy, d.mdy].find((c) => +c.slice(5, 7) === matching[0].month);
      return { value: pick, method: "month_stated_in_bill_number", evidenceText: `The bill number prints the month '${matching[0].text}', which agrees with exactly one reading.`, evidenceLine: matching[0].line };
    }
    return { value: null, method: "unresolved", evidenceText: null };
  };
  for (const d of pool) Object.assign(d, { res: resolve(d) });
  const pri = (d) => (d.cls === "transaction" ? (/\b(SALE\s*DATE|TRANSACTION\s*DATE|TXN\s*DATE|BILL\s*DATE|INVOICE\s*DATE|DATE)\b/i.test(d.labelText) ? 3 : 2) : 1);
  const top = Math.max(...pool.map(pri));
  const topPool = pool.filter((d) => pri(d) === top);
  const evidence = topPool.map((d) => lineEvidence(d.line, d.cls === "transaction" ? "transaction_date_line" : "date_line"));
  const resolvedValues = new Set(topPool.filter((d) => d.res.value).map((d) => d.res.value));
  const unresolved = topPool.filter((d) => !d.res.value);
  const rawAll = [...new Set(topPool.map((d) => d.raw))];
  const printNote = printed.length && printed.some((p) => !topPool.some((d) => d.res.value && p.dmy === d.res.value)) ? "A print-date is also printed and was not used." : null;
  if (printed.length) evidence.push(...printed.map((d) => lineEvidence(d.line, "print_date_not_used")));
  let ambiguity = null;
  let value = null;
  let method = "unresolved";
  let review = true;
  let evidenceNote = null;
  if (resolvedValues.size > 1) {
    ambiguity = `Conflicting dates on the receipt: ${[...resolvedValues].join(" vs ")}.`;
    warnings.push(ambiguity);
  } else if (resolvedValues.size === 1 && unresolved.length === 0) {
    value = [...resolvedValues][0];
    const r = topPool.find((d) => d.res.value === value).res;
    method = r.method;
    review = method === "month_stated_in_bill_number" || topPool.some((d) => d.repaired) ? true : false;
    evidenceNote = r.evidenceText;
    if (r.evidenceLine) evidence.push(lineEvidence(r.evidenceLine, "month_token_in_bill_number"));
    if (repairedUsed) {
      review = true;
      ambiguity = "The date was read only after correcting letter/digit OCR confusions; verify.";
    }
    if (method === "month_stated_in_bill_number") {
      review = false; // explicit month printed on the receipt, agreeing with exactly one reading
    }
  } else if (resolvedValues.size === 1 && unresolved.length) {
    // some candidates are still ambiguous; use the resolved one only if every ambiguous reading CAN equal it
    const v = [...resolvedValues][0];
    if (unresolved.every((d) => d.candidates.includes(v))) {
      value = v;
      method = topPool.find((d) => d.res.value === v).res.method;
      review = false;
      evidenceNote = "Another reading of an ambiguous transaction stamp is consistent with it.";
    } else {
      ambiguity = `Date candidates disagree: ${[v, ...unresolved.flatMap((d) => d.candidates)].join(" / ")}.`;
    }
  } else {
    const cands = [...new Set(unresolved.flatMap((d) => d.candidates))];
    ambiguity = `The day/month order of '${rawAll[0]}' cannot be established from this receipt (${cands.join(" or ")}); nothing on it proves the order.`;
    warnings.push(ambiguity);
  }
  const lineConf = topPool.map((d) => d.line.conf).filter((x) => typeof x === "number");
  const minConf = lineConf.length ? Math.min(...lineConf) : null;
  if (value && minConf !== null && minConf < 0.55) {
    review = true;
    ambiguity = (ambiguity ? ambiguity + " " : "") + `Low OCR confidence (${minConf.toFixed(2)}) on the date line.`;
  }
  if (value && topPool.some((d) => d.line.edge && (d.line.edge.left || d.line.edge.right))) {
    review = true;
    ambiguity = (ambiguity ? ambiguity + " " : "") + "The date line touches the edge of the image, so digits may be cut off.";
  }
  const tokConfs = topPool.map((d) => tokenConfidence(d.line, d.raw)).filter((x) => x !== null);
  if (value && tokConfs.length && Math.min(...tokConfs) < TOKEN_MIN_CONF) {
    review = true;
    ambiguity = (ambiguity ? ambiguity + " " : "") + `The date digits were read with OCR confidence ${Math.min(...tokConfs).toFixed(2)}.`;
  }
  return {
    raw_value: rawAll.join(" | "),
    normalized_value: value,
    confidence: value ? round2(Math.min(0.95, (minConf ?? 0.7) * (review ? 0.7 : 1) + (method === "iso" || method === "month_name" ? 0.05 : 0))) : null,
    confidence_type: "heuristic_uncalibrated",
    evidence,
    ambiguity: [ambiguity, evidenceNote, printNote].filter(Boolean).join(" ") || null,
    review_required: review || value === null,
    candidates: [...new Set(topPool.flatMap((d) => d.candidates))],
    basis: method,
  };
}

// OCR confidence of the single word that carries a number/date, which a high LINE confidence can hide ("500.60" read at 0.61 on an 0.85 line).
function tokenConfidence(line, raw) {
  const want = String(raw).replace(/[^0-9]/g, "");
  if (!want || !line.words?.length) return null;
  const hits = line.words.filter((w) => typeof w.confidence === "number" && String(w.text).replace(/[^0-9]/g, "").includes(want.slice(0, Math.min(want.length, 6))));
  return hits.length ? Math.min(...hits.map((w) => w.confidence)) : null;
}
const TOKEN_MIN_CONF = 0.7; // heuristic gate chosen on DEVELOPMENT data only; not a calibrated probability

function labelFor(text, index, prevLine) {
  const before = text.slice(0, index);
  const lab = before.slice(-24);
  if (lab.trim().length >= 2) return before.slice(-40);
  return (prevLine?.text || "").slice(-30) + " " + before;
}

function nullField(name, why) {
  return { raw_value: null, normalized_value: null, confidence: null, confidence_type: "heuristic_uncalibrated", evidence: [], ambiguity: why, review_required: true, basis: null };
}

// ----------------------------------------------------------------------------------------------- amount
const AMT_POS = [
  [5, /\b(GRAND\s*TOTAL|TOTAL\s*AMOUNT|TOTAL\s*AMT|NET\s*AMOUNT|NET\s*AMT|SALE\s*AMOUNT|SALE\s*AMT|AMOUNT\s*PAID|AMOUNT\s*PAYABLE|TOTAL\s*\(?\s*INR\s*\)?|TOTAL\s*\(?\s*RS\.?\s*\)?)\b/i],
  [4, /\b(AMOUNT\s*\(?\s*RS\.?\s*\)?|AMOUNT|AMT)\b/i],
  [3, /\b(SALE|TOTAL|VALUE\s*OF\s*SALE)\b/i],
];
// Labels that introduce a number which is NOT the transaction total. They are only used to avoid mistaking such numbers.
const AMT_NEG = /\b(RATE|PRICE|UNIT|VOL|VOLUME|QTY|QUANTITY|LTR|LITRES?|DENSITY|ATOT|VTOT|BALANCE|POINTS?|REWARD[S]?|LOYALTY|CHANGE|PRESET|TIN|GST(IN)?|CST|LST|VAT|INVOICE|INV|BILL\s*NO|RECEIPT\s*NO|TXN|TRNS|TRANS|ID|NOZZLE|NOZL|FIP|FCC|TEL|PH|PHONE|MOBILE|MOB|VEHICLE|VEH|TENDER(ED)?|GIVEN|SUBTOTAL|SUB\s*TOTAL|DISCOUNT|TAX)\b|\/\s*L\b|KG\s*\/|CU\.?\s*MTR/i;
const MONEY = /(?:(?:R[sS5]{1,2}\.?|INR|\u20b9)\s*[:\-]?\s*)?(\d{1,3}(?:,\d{2,3})+(?:\.\d{1,2})?|\d+(?:\.\d{1,2})?)(?:\s*(?:INR|\/-))?/gi;
// "2000. G0" / "1500.O0": digits followed by a decimal point whose cents contain letter/digit OCR confusions. Always flagged repaired.
const MONEY_REPAIRABLE = /(\d{3,})\.\s*([0-9OoGgDQ]{2})(?![0-9A-Za-z])/g;

function moneyTokens(text, { repair = false } = {}) {
  const out = [];
  for (const m of text.matchAll(MONEY)) {
    const numText = m[1];
    const digits = numText.replace(/,/g, "");
    const val = Number(digits);
    if (!Number.isFinite(val)) continue;
    // reject things that are clearly not money: very long digit strings (ids), times
    if (digits.replace(".", "").length > 9) continue;
    const prefixed = /(R[sS5]{1,2}\.?|INR|\u20b9)/i.test(m[0]);
    const decimal = /\.\d{2}$/.test(numText);
    out.push({ raw: m[0].trim(), index: m.index, value: val, prefixed, decimal, numText, repaired: false });
  }
  if (repair && !out.some((t) => t.decimal)) {
    for (const m of text.matchAll(MONEY_REPAIRABLE)) {
      const cents = m[2].replace(/[OoGgDQ]/g, "0");
      if (!/^\d\d$/.test(cents)) continue;
      // the repaired reading supersedes the bare integer it was cut from ("2000" of "2000. G0")
      for (let i = out.length - 1; i >= 0; i--) if (out[i].index >= m.index && out[i].index < m.index + m[0].length) out.splice(i, 1);
      out.push({ raw: m[0].trim(), index: m.index, value: Number(`${m[1]}.${cents}`), prefixed: false, decimal: true, numText: `${m[1]}.${cents}`, repaired: true });
    }
    out.sort((a, b) => a.index - b.index);
  }
  return out;
}

// Small bare integers ("1:" in a garbled "(Rss. 1: 2000.G0") are not amounts.
const sensibleAmountToken = (t) => t.repaired || t.decimal || /\/-/.test(t.raw) || t.numText.replace(/\D/g, "").length >= 3;

function levenshtein(a, b) {
  const dp = Array.from({ length: a.length + 1 }, (_, i) => [i, ...Array(b.length).fill(0)]);
  for (let j = 1; j <= b.length; j++) dp[0][j] = j;
  for (let i = 1; i <= a.length; i++) for (let j = 1; j <= b.length; j++) dp[i][j] = Math.min(dp[i - 1][j] + 1, dp[i][j - 1] + 1, dp[i - 1][j - 1] + (a[i - 1] === b[j - 1] ? 0 : 1));
  return dp[a.length][b.length];
}
// An OCR-mangled "Amount" label ("fnount(Rs.)"): similar enough to AMOUNT AND followed by a currency-unit hint. Always review-flagged.
function fuzzyAmountLabel(text) {
  for (const m of text.matchAll(/([A-Za-z]{5,8})\s*\(?\s*R[sS5]/g)) {
    if (levenshtein(m[1].toUpperCase(), "AMOUNT") <= 2) return { index: m.index, text: m[1], end: m.index + m[1].length };
  }
  return null;
}

function totalAmount(lines, warnings) {
  const cands = [];
  lines.forEach((l, k) => {
    let labelMatch = AMT_POS.map(([p, re]) => ({ p, m: re.exec(l.text) })).filter((x) => x.m);
    let fuzzy = false;
    if (!labelMatch.length) {
      const fz = fuzzyAmountLabel(l.text);
      if (!fz) return;
      labelMatch = [{ p: 4, m: Object.assign([fz.text], { index: fz.index }) }];
      fuzzy = true;
    }
    const best = labelMatch.sort((a, b) => b.p - a.p)[0];
    // negative words on the SAME line before the label disqualify (e.g. "Preset Amount", "Unit Price")
    const afterLabel = l.text.slice(best.m.index + best.m[0].length);
    const beforeLabel = l.text.slice(0, best.m.index);
    if (AMT_NEG.test(beforeLabel) && !/:\s*$/.test(beforeLabel)) return;
    // a "Total Volume"-style line
    if (/\b(VOL|VOLUME|LTR|LITRE)\b/i.test(best.m[0] + " " + beforeLabel)) return;
    let tokens = moneyTokens(afterLabel, { repair: true }).filter((t) => !AMT_NEG.test(afterLabel.slice(0, t.index))).filter(sensibleAmountToken);
    let from = l;
    if (!tokens.length && lines[k + 1]) {
      const nxt = lines[k + 1];
      // value printed on the next line (label/value column split) - only if that line carries no label of its own
      if (!AMT_NEG.test(nxt.text.replace(/[\d.,\s]/g, "")) && !/[A-Za-z]{3,}/.test(nxt.text.replace(/RS|INR|AMOUNT/gi, ""))) {
        tokens = moneyTokens(nxt.text);
        from = nxt;
      }
    }
    if (!tokens.length) return;
    const t = tokens[0];
    cands.push({ priority: best.p, label: best.m[0], token: t, line: from, labelLine: l, k, fuzzy, repaired: Boolean(t.repaired) });
  });
  if (!cands.length) {
    // no labelled amount: a single currency-prefixed token is a weak candidate that always needs review
    const loose = [];
    for (const l of lines) for (const t of moneyTokens(l.text)) if (t.prefixed && !AMT_NEG.test(l.text.slice(0, t.index))) loose.push({ t, l });
    const distinct = new Set(loose.map((x) => x.t.value));
    if (distinct.size === 1) {
      const x = loose[0];
      warnings.push("Amount found without a total/amount label; verify.");
      return field(x.t.raw, x.t.value, [lineEvidence(x.l, "unlabelled_currency_amount")], "Printed without an amount/total label.", true, "unlabelled_currency_token", x.l.conf);
    }
    warnings.push(distinct.size ? "Several unlabelled currency amounts; the total is not identifiable." : "No total amount could be read.");
    return { ...nullField("total_amount", distinct.size ? `Unlabelled candidates: ${[...distinct].join(", ")}.` : "No amount text found."), candidates: [...distinct] };
  }
  const topP = Math.max(...cands.map((c) => c.priority));
  const top = cands.filter((c) => c.priority === topP);
  const values = [...new Set(top.map((c) => round2(c.token.value)))];
  const allValues = [...new Set(cands.map((c) => round2(c.token.value)))];
  const evidence = top.map((c) => lineEvidence(c.line, "amount_line"));
  if (values.length > 1) {
    const amb = `Several different amounts carry the same label priority: ${values.join(", ")}.`;
    warnings.push(amb);
    return { ...nullField("total_amount", amb), raw_value: top.map((c) => c.token.raw).join(" | "), evidence, candidates: allValues };
  }
  const chosen = top[0];
  const value = values[0];
  // conflicting currency amounts elsewhere that are not excluded by their own labels (e.g. a hand-written figure beside the total)
  const others = new Set();
  for (const l of lines) {
    if (AMT_NEG.test(l.text)) continue;
    for (const t of moneyTokens(l.text)) if (t.prefixed && Math.abs(t.value - value) > 0.005 && !cands.some((c) => c.line === l)) others.add(t.value);
  }
  let review = false;
  let ambiguity = null;
  const lowerCands = allValues.filter((v) => Math.abs(v - value) > 0.005);
  if (others.size) {
    review = true;
    ambiguity = `Another currency amount appears on the receipt that is not explained by a rate/quantity/ID label: ${[...others].join(", ")}.`;
    warnings.push(ambiguity);
  }
  if (lowerCands.length) {
    review = true;
    ambiguity = (ambiguity ? ambiguity + " " : "") + `A lower-priority label carries a different amount: ${lowerCands.join(", ")}.`;
  }
  if (value < 1 || value > 500000) {
    review = true;
    ambiguity = (ambiguity ? ambiguity + " " : "") + "Amount outside the plausible range for a single fuel purchase.";
  }
  if (!chosen.token.decimal && !chosen.token.prefixed && !/\/-/.test(chosen.token.raw)) {
    // a bare integer without currency marker or decimals is weaker evidence
    review = review || chosen.token.value < 20;
  }
  if (chosen.fuzzy) {
    review = true;
    ambiguity = (ambiguity ? ambiguity + " " : "") + `The amount label was only recognised approximately ('${chosen.label}').`;
  }
  if (chosen.repaired) {
    review = true;
    ambiguity = (ambiguity ? ambiguity + " " : "") + "The cents were read only after correcting letter/digit OCR confusions; verify.";
  }
  const conf = chosen.line.conf;
  if (typeof conf === "number" && conf < 0.55) {
    review = true;
    ambiguity = (ambiguity ? ambiguity + " " : "") + `Low OCR confidence (${conf.toFixed(2)}) on the amount line.`;
  }
  if (chosen.line.edge && (chosen.line.edge.left || chosen.line.edge.right)) {
    review = true;
    ambiguity = (ambiguity ? ambiguity + " " : "") + "The amount line touches the edge of the image, so digits may be cut off.";
  }
  const tokConf = tokenConfidence(chosen.line, chosen.token.raw);
  if (tokConf !== null && tokConf < TOKEN_MIN_CONF) {
    review = true;
    ambiguity = (ambiguity ? ambiguity + " " : "") + `The amount digits were read with OCR confidence ${tokConf.toFixed(2)}.`;
  }
  const corroboration = cands.filter((c) => Math.abs(c.token.value - value) < 0.005).length;
  return {
    raw_value: chosen.token.raw,
    normalized_value: value,
    confidence: round2(Math.min(0.97, (conf ?? 0.7) * (review ? 0.7 : 1) + (corroboration > 1 ? 0.05 : 0))),
    confidence_type: "heuristic_uncalibrated",
    evidence,
    ambiguity,
    review_required: review,
    candidates: allValues,
    basis: `label:${chosen.label.toLowerCase()}`,
  };
}

function field(raw, value, evidence, ambiguity, review, basis, lineConf) {
  return { raw_value: raw, normalized_value: value, confidence: round2(Math.min(0.6, (lineConf ?? 0.5) * 0.6)), confidence_type: "heuristic_uncalibrated", evidence, ambiguity, review_required: review, basis };
}

// ----------------------------------------------------------------------------------------------- public interface
export function extractFuelReceipt(ocr, opts = {}) {
  const warnings = [];
  const lines = buildLines(ocr);
  const fullText = lines.map((l) => l.text).join("\n");
  const fuelWords = lines.filter((l) => FUEL_WORDS.test(l.text)).length;
  const fuelLabels = lines.filter((l) => FUEL_LABELS.test(l.text)).length;
  const brand = detectBrand(lines, false);
  const hasFuelEvidence = fuelLabels >= 2 || (fuelWords >= 1 && fuelLabels >= 1) || (fuelWords >= 2) || Boolean(brand && fuelWords >= 1);
  const notes = [];
  if (!lines.length) warnings.push("OCR returned no text.");
  if (!hasFuelEvidence) notes.push("No fuel-receipt evidence (no fuel product / pump-machine labels / oil-company brand with fuel words).");
  const pump = pumpName(lines, hasFuelEvidence, warnings);
  const date = transactionDate(lines, fullText, warnings);
  const amount = totalAmount(lines, warnings);
  if (!hasFuelEvidence) {
    for (const f of [pump, date, amount]) {
      f.review_required = true;
      f.ambiguity = [f.ambiguity, "No fuel-receipt evidence on this document."].filter(Boolean).join(" ");
    }
    warnings.push(...notes);
  }
  const fields = { pump_name: pump, transaction_date: date, total_amount: amount };
  return {
    ...fields,
    validation_warnings: warnings,
    overall_review_required: Object.values(fields).some((f) => f.review_required || f.normalized_value === null) || !hasFuelEvidence,
    _meta: { implementation: VERSION, fuel_evidence: hasFuelEvidence, ocr_source: ocr.source, ocr_mean_confidence: ocr.confidence },
  };
}
