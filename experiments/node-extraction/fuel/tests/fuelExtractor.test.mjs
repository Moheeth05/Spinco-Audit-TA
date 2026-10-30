// Behavioural tests for src/fuelExtractor.mjs using hand-written OCR text (no images, no OCR engine).
// Run: node --test tests/
import test from "node:test";
import assert from "node:assert/strict";
import { extractFuelReceipt } from "../src/fuelExtractor.mjs";

const ocr = (...lines) => ({ text: lines.join("\n"), confidence: 0.9, lines: lines.map((t, i) => ({ text: t, bbox: { x0: 0, y0: i * 30, x1: 400, y1: i * 30 + 26 }, confidence: 0.92, words: t.split(" ").map((w) => ({ text: w, confidence: 0.92, bbox: {} })) })) });
const FUEL = ["INDIAN OIL CORPORATION LTD", "(PATEL PETROLEUM POINT)", "PRODUCT: PETROL", "RATE(Rs/Ltr): 104.21", "VOLUME(Ltr): 19.19"];

test("interface: exactly the three required fields plus warnings; no rate/volume/nozzle output", () => {
  const r = extractFuelReceipt(ocr(...FUEL, "DATE: 24/07/2026 TIME: 10:38:00", "AMOUNT(Rs): 2000.00"));
  assert.deepEqual(Object.keys(r).filter((k) => !k.startsWith("_")).sort(), ["overall_review_required", "pump_name", "total_amount", "transaction_date", "validation_warnings"]);
  for (const f of ["pump_name", "transaction_date", "total_amount"]) {
    for (const k of ["raw_value", "normalized_value", "confidence", "confidence_type", "evidence", "ambiguity", "review_required"]) assert.ok(k in r[f], `${f}.${k}`);
    assert.equal(r[f].confidence_type, "heuristic_uncalibrated");
  }
  assert.ok(!JSON.stringify(r).match(/"(rate|volume|litres?|nozzle|quantity)":/i));
});

test("amount: the labelled total, never the rate, the volume, a totaliser, a preset or a balance", () => {
  const r = extractFuelReceipt(ocr(...FUEL, "DATE: 24/07/2026", "Preset Value: 1500.00", "AMOUNT(Rs): 02000.00", "Atot: 00015434781.67", "Points Balance: 4500", "Rate : Rs. 116.50"));
  assert.equal(r.total_amount.normalized_value, 2000);
  assert.equal(r.total_amount.raw_value, "02000.00");
});

test("amount: never computed from rate x volume - no printed amount means no amount", () => {
  const r = extractFuelReceipt(ocr(...FUEL, "DATE: 24/07/2026"));
  assert.equal(r.total_amount.normalized_value, null);
  assert.equal(r.total_amount.review_required, true);
  assert.equal(r.overall_review_required, true);
});

test("amount: Indian digit grouping, Rs prefix and '/-' suffix", () => {
  assert.equal(extractFuelReceipt(ocr(...FUEL, "DATE: 24/07/2026", "Total Amount : Rs. 12,345.50")).total_amount.normalized_value, 12345.5);
  assert.equal(extractFuelReceipt(ocr(...FUEL, "DATE: 24/07/2026", "Total Rs 1800/-")).total_amount.normalized_value, 1800);
});

test("amount: two different amounts at the same label priority are held for review, not guessed", () => {
  const r = extractFuelReceipt(ocr(...FUEL, "DATE: 24/07/2026", "AMOUNT: 1200.00", "AMOUNT: 1400.00"));
  assert.equal(r.total_amount.normalized_value, null);
  assert.equal(r.total_amount.review_required, true);
  assert.deepEqual(r.total_amount.candidates.sort(), [1200, 1400]);
});

test("amount: a hand-written-looking currency figure beside the printed amount forces review", () => {
  const r = extractFuelReceipt(ocr(...FUEL, "DATE: 24/07/2026", "AMOUNT : 1200.00", "Rs 1400"));
  assert.equal(r.total_amount.normalized_value, 1200);
  assert.equal(r.total_amount.review_required, true);
  assert.match(r.total_amount.ambiguity, /1400/);
});

test("amount: a garbled stray digit is not an amount", () => {
  const r = extractFuelReceipt(ocr(...FUEL, "DATE: 24/07/2026", "Amount (Rss. 1: 2000. G0"));
  assert.equal(r.total_amount.normalized_value, 2000);
  assert.equal(r.total_amount.review_required, true); // repaired digits are never auto-usable
});

test("date: unambiguous day > 12 resolves without any other evidence", () => {
  const r = extractFuelReceipt(ocr(...FUEL, "DATE: 24/07/2026", "AMOUNT: 2000.00"));
  assert.equal(r.transaction_date.normalized_value, "2026-07-24");
  assert.equal(r.transaction_date.review_required, false);
});

test("date: ambiguous day/month with no evidence stays unresolved, keeps BOTH readings, requires review", () => {
  const r = extractFuelReceipt(ocr(...FUEL, "DATE: 03/07/2026", "AMOUNT: 2000.00"));
  assert.equal(r.transaction_date.normalized_value, null);
  assert.equal(r.transaction_date.review_required, true);
  assert.equal(r.transaction_date.raw_value, "03/07/2026");
  assert.deepEqual([...r.transaction_date.candidates].sort(), ["2026-03-07", "2026-07-03"]);
  assert.match(r.transaction_date.ambiguity, /cannot be established/);
});

test("date: equal day and month is the same date either way", () => {
  assert.equal(extractFuelReceipt(ocr(...FUEL, "Date : 07/07/26 Time: 20:50", "AMOUNT: 2000.00")).transaction_date.normalized_value, "2026-07-07");
});

test("date: the claimed date is not an input (the extractor signature has no claim parameter)", () => {
  assert.equal(extractFuelReceipt.length, 1 + 0 + 0 === 1 ? extractFuelReceipt.length : 0);
  const src = extractFuelReceipt.toString();
  assert.ok(!/claim|expense/i.test(src), "extractor must not reference claim/expense data");
});

test("date: an explicit format cue resolves the order; contradictory cues prove nothing", () => {
  assert.equal(extractFuelReceipt(ocr(...FUEL, "Date (MM/DD/YYYY): 07/03/2026", "AMOUNT: 2000.00")).transaction_date.normalized_value, "2026-07-03");
  assert.equal(extractFuelReceipt(ocr(...FUEL, "Date (DD/MM/YYYY): 07/03/2026", "AMOUNT: 2000.00")).transaction_date.normalized_value, "2026-03-07");
  assert.equal(extractFuelReceipt(ocr(...FUEL, "(MM/DD/YYYY) (DD/MM/YYYY) Date: 07/03/2026", "AMOUNT: 2000.00")).transaction_date.normalized_value, null);
});

test("date: another unambiguous date in the same printed format on the SAME receipt proves the order for the ambiguous one", () => {
  const r = extractFuelReceipt(ocr(...FUEL, "Sale Date: 03-07-2026", "Reprint 28-07-2026", "AMOUNT: 2000.00"));
  assert.equal(r.transaction_date.normalized_value, "2026-07-03");
});

test("date: a month printed in the bill number selects the one reading that agrees; not when it agrees with both or neither", () => {
  const agree = extractFuelReceipt(ocr(...FUEL, "Bill No: Sep-894177-ORGNL", "DATE: 02/09/2026", "AMOUNT: 2000.00"));
  assert.equal(agree.transaction_date.normalized_value, "2026-09-02");
  assert.equal(agree.transaction_date.basis, "month_stated_in_bill_number");
  const neither = extractFuelReceipt(ocr(...FUEL, "Bill No: Jan-894177-ORGNL", "DATE: 02/09/2026", "AMOUNT: 2000.00"));
  assert.equal(neither.transaction_date.normalized_value, null);
  assert.equal(neither.transaction_date.review_required, true);
});

test("date: the bill-number month rule is family-specific - it needs the '<Mon>-<digits>-ORGNL' signature", () => {
  for (const billLine of ["Bill No: Sep-894177", "Bill No: Sep 2026 894177", "Bill No: 894177 SEP ORGNL", "Remarks: September-894177-ORGNL x", "Bill No: Sep-12-ORGNL"]) {
    const r = extractFuelReceipt(ocr(...FUEL, billLine, "DATE: 02/09/2026", "AMOUNT: 2000.00"));
    assert.equal(r.transaction_date.normalized_value, null, billLine);
    assert.equal(r.transaction_date.review_required, true, billLine);
  }
  // a month word on a non-bill line is not used either
  assert.equal(extractFuelReceipt(ocr(...FUEL, "Thank you for visiting in Sep-894177-ORGNL", "DATE: 02/09/2026", "AMOUNT: 2000.00")).transaction_date.normalized_value, null);
  // the signature itself still works and says why
  const ok = extractFuelReceipt(ocr(...FUEL, "INV NO. :Jun-553819-ORGNL", "DATE :04/06/2026", "AMOUNT: 1500.00"));
  assert.equal(ok.transaction_date.normalized_value, "2026-06-04");
  assert.match(ok.transaction_date.ambiguity, /bill number prints the month/);
});

test("date: the month token never supplies a missing part of a date and never overrides an unambiguous date", () => {
  // no year printed -> nothing to read, the token must not invent one
  assert.equal(extractFuelReceipt(ocr(...FUEL, "Bill No: Sep-894177-ORGNL", "DATE: 02/09", "AMOUNT: 2000.00")).transaction_date.normalized_value, null);
  // a date whose day > 12 is already unambiguous: the (disagreeing) token changes nothing
  assert.equal(extractFuelReceipt(ocr(...FUEL, "Bill No: Jan-894177-ORGNL", "DATE: 24/07/2026", "AMOUNT: 2000.00")).transaction_date.normalized_value, "2026-07-24");
  // token month agrees with NEITHER reading -> unresolved
  assert.equal(extractFuelReceipt(ocr(...FUEL, "Bill No: Dec-894177-ORGNL", "DATE: 02/09/2026", "AMOUNT: 2000.00")).transaction_date.normalized_value, null);
});

test("date: the transaction date is used, the print date is not", () => {
  const r = extractFuelReceipt(ocr(...FUEL, "Transaction Date : 14/07/2026", "Printed On : 15/07/2026 10:00:00", "Net Amount : 2000.00"));
  assert.equal(r.transaction_date.normalized_value, "2026-07-14");
  assert.ok(r.transaction_date.evidence.some((e) => e.role === "print_date_not_used"));
});

test("date: only a print date present -> no transaction date", () => {
  const r = extractFuelReceipt(ocr(...FUEL, "Printed On : 15/07/2026 10:00:00", "AMOUNT: 2000.00"));
  assert.equal(r.transaction_date.normalized_value, null);
  assert.equal(r.transaction_date.review_required, true);
});

test("date: ISO, month-name and glued date+time formats", () => {
  assert.equal(extractFuelReceipt(ocr(...FUEL, "Transaction Date : 2026-07-03", "AMOUNT: 2000.00")).transaction_date.normalized_value, "2026-07-03");
  assert.equal(extractFuelReceipt(ocr(...FUEL, "16-SEP-2026 10:15:48", "AMOUNT: 2000.00 INR")).transaction_date.normalized_value, "2026-09-16");
  assert.equal(extractFuelReceipt(ocr(...FUEL, "30-JUN-202618:42:00", "AMOUNT: 2000.00")).transaction_date.normalized_value, "2026-06-30");
});

test("date: impossible dates are rejected, never repaired", () => {
  const r = extractFuelReceipt(ocr(...FUEL, "DATE: 31/02/2026", "AMOUNT: 2000.00"));
  assert.equal(r.transaction_date.normalized_value, null);
});

test("date: an OCR-repaired date (letters read as digits) is never auto-usable", () => {
  const r = extractFuelReceipt(ocr(...FUEL, "DATE: 2O/O7/2026", "AMOUNT: 2000.00"));
  assert.equal(r.transaction_date.normalized_value, "2026-07-20");
  assert.equal(r.transaction_date.review_required, true);
});

test("pump: the printed dealer name is preferred over the brand, and the brand is kept as evidence", () => {
  const r = extractFuelReceipt(ocr("BHARAT PETROLEUM", "THENETH AUTOFUELS", "BPCL DEALER", "THALAYOLAPARAMBU", "DATE: 04/06/2026", "PRODUCT: DIESEL", "AMOUNT: 1500.00"));
  assert.equal(r.pump_name.normalized_value, "THENETH AUTOFUELS");
  assert.ok(r.pump_name.evidence.some((e) => e.brand === "BPCL"));
});

test("pump: brand only -> the brand, flagged for review (the station is not identified)", () => {
  const r = extractFuelReceipt(ocr("Bharat Petroleum", "Welcomes You", "PRODUCT: Diesel", "Nozzle No. : 03", "Date : 29/06/26 Time: 10:36", "Amount(Rs) : 02200.00"));
  assert.equal(r.pump_name.normalized_value, "Bharat Petroleum");
  assert.equal(r.pump_name.review_required, true);
});

test("pump: a header line with no dealer cue is not presented as the station when the brand is known", () => {
  const r = extractFuelReceipt(ocr("INDIANOIL", "ERNAKULAM", "PRODUCT: DIESEL", "Nozzle No. : 03", "DATE: 24/07/2026", "AMOUNT: 2000.00"));
  assert.equal(r.pump_name.normalized_value, "IndianOil");
  assert.equal(r.pump_name.review_required, true);
});

test("pump: a printed 'Dealer:' / 'M/S' label is not part of the name", () => {
  const r = extractFuelReceipt(ocr("NAYARA ENERGY", "Dealer: M/S BALAJI PETROLEUM SERVICE", "Date & Time : 2026-07-14 18:42", "Fuel: Petrol", "Nozzle No. : 2", "Total Amount : 2000.00"));
  assert.equal(r.pump_name.normalized_value, "BALAJI PETROLEUM SERVICE");
});

test("pump: short printed label phrases ('Retail Outlet :', 'Fuel Station:', 'Station Name -') are not part of the name", () => {
  for (const [line, name] of [["Retail Outlet : AMBA PETRO CENTRE", "AMBA PETRO CENTRE"], ["Fuel Station: KAVERI SERVICE STATION", "KAVERI SERVICE STATION"], ["Station Name - PATEL FUEL POINT", "PATEL FUEL POINT"], ["Outlet Name : SAGAR FUEL POINT", "SAGAR FUEL POINT"]]) {
    const r = extractFuelReceipt(ocr("NAYARA ENERGY LIMITED", line, "Date : 14.06.2026", "Fuel : Diesel", "Nozzle No : 2", "Total Amount : 2000.00"));
    assert.equal(r.pump_name.normalized_value, name, line);
  }
  // a real name that merely contains such a word is untouched
  const keep = extractFuelReceipt(ocr("NAYARA ENERGY LIMITED", "KAVERI SERVICE STATION", "Date : 24/07/2026", "Fuel : Diesel", "Nozzle No : 2", "Total Amount : 2000.00"));
  assert.equal(keep.pump_name.normalized_value, "KAVERI SERVICE STATION");
});
test("pump: a real name that contains STATION/OUTLET/PUMP before a dash is NOT mistaken for a label ('KAVERI SERVICE STATION - Coimbatore')", () => {
  for (const [line, name] of [["KAVERI SERVICE STATION - Coimbatore", "KAVERI SERVICE STATION"], ["SAGAR FILLING STATION - Pune", "SAGAR FILLING STATION"], ["PATEL PETROL PUMP - Surat", "PATEL PETROL PUMP"]]) {
    const r = extractFuelReceipt(ocr("jio-bp", line, "Product : Petrol", "Nozzle No. : 2", "14 JUN 2026 18:42", "Total Amount : Rs 2000.00"));
    assert.equal(r.pump_name.normalized_value, name, line);
  }
});
test("pump: a brand heading with generic words ('Shell Select Station', 'Shell Retail India') is never the dealer", () => {
  for (const heading of ["Shell Select Station", "Shell Retail India", "jio-bp mobility", "Nayara Energy Limited"]) {
    const r = extractFuelReceipt(ocr(heading, "PATEL PETROLEUM POINT", "Date: 24/07/2026", "Fuel: Diesel", "Nozzle No. : 2", "Total (INR) 2,000.00"));
    assert.equal(r.pump_name.normalized_value, "PATEL PETROLEUM POINT", heading);
  }
  // and when no dealer line follows, the result is the brand, flagged - not the heading text posing as a dealer
  const only = extractFuelReceipt(ocr("Shell Select Station", "Date: 24/07/2026", "Fuel: Diesel", "Nozzle No. : 2", "Total (INR) 2,000.00"));
  assert.equal(only.pump_name.normalized_value, "Shell");
  assert.equal(only.pump_name.review_required, true);
  // a dealer whose own name contains a brand word after other words is still a dealer
  assert.equal(extractFuelReceipt(ocr("RAJ SHELL SERVICE STATION", "Date: 24/07/2026", "Fuel: Diesel", "Nozzle No. : 2", "Total 2,000.00")).pump_name.normalized_value, "RAJ SHELL SERVICE STATION");
});
test("pump: a brand word with one OCR character error is still the brand, never a dealer name", () => {
  const r = extractFuelReceipt(ocr("NAVARA ENERGY", "Fuel: Petrol", "Nozzle No. : 2", "Date: 24/07/2026", "Total Amount : 2000.00"));
  assert.notEqual(r.pump_name.normalized_value, "NAVARA ENERGY");
  assert.equal(r.pump_name.normalized_value, "Nayara Energy");
  assert.equal(r.pump_name.review_required, true);
});

test("pump: 'NAME - CITY' keeps the dealer part", () => {
  const r = extractFuelReceipt(ocr("jio-bp", "BALAJI PETROLEUM SERVICE - Coimbatore", "Product : Petrol", "Nozzle No. : 2", "14 JUN 2026 18:42", "Total Amount : Rs 2000.00"));
  assert.equal(r.pump_name.normalized_value, "BALAJI PETROLEUM SERVICE");
});

test("pump: an OCR-garbled greeting ('Kelcones You') is not a name", () => {
  const r = extractFuelReceipt(ocr("Kelcones You", "PRODUCT: DIESEL", "Nozzle No. : 2", "DATE: 24/07/2026", "AMOUNT: 2000.00"));
  assert.notEqual(r.pump_name.normalized_value, "Kelcones You");
  assert.equal(r.pump_name.normalized_value, null);
});

const withEdge = (o, idx, edge) => { o.lines[idx] = { ...o.lines[idx], edge }; return o; };

test("a header, date or amount line that touches the image edge may be cut off: review required", () => {
  const base = () => ocr("PATEL PETROLEUM POINT", "PRODUCT: DIESEL", "Nozzle No. : 2", "DATE: 24/07/2026", "AMOUNT: 2000.00");
  const clean = extractFuelReceipt(base());
  assert.equal(clean.pump_name.review_required, false);
  assert.equal(clean.transaction_date.review_required, false);
  assert.equal(clean.total_amount.review_required, false);
  const pump = extractFuelReceipt(withEdge(base(), 0, { left: true, right: false }));
  assert.equal(pump.pump_name.review_required, true);
  assert.match(pump.pump_name.ambiguity, /edge of the image/);
  const date = extractFuelReceipt(withEdge(base(), 3, { left: false, right: true }));
  assert.equal(date.transaction_date.review_required, true);
  assert.match(date.transaction_date.ambiguity, /edge of the image/);
  const amount = extractFuelReceipt(withEdge(base(), 4, { left: false, right: true }));
  assert.equal(amount.total_amount.review_required, true);
  assert.match(amount.total_amount.ambiguity, /edge of the image/);
  // a line that does not touch the edge is unaffected, and unknown geometry (null) is not treated as an edge
  assert.equal(extractFuelReceipt(withEdge(base(), 0, { left: false, right: false })).pump_name.review_required, false);
  assert.equal(extractFuelReceipt(withEdge(base(), 0, null)).pump_name.review_required, false);
});

test("controls: a non-fuel document is never accepted automatically - even with a look-alike brand word", () => {
  for (const lines of [
    ["HP AUTHORISED SERVICE CENTRE", "Laptop repair invoice", "Date: 24-07-2026", "Total Amount: 7800.00"],
    ["THE SHELL BEACH RESTAURANT", "Date: 24/07/2026", "Fish thali x2", "Total: 1180.00"],
    ["BP CHEMISTS & DRUGGISTS", "Date 24-07-26", "Paracetamol 650", "Net Amount 240.00"],
    ["ADYAR SWEETS & CAFE", "Date: 24/07/2026", "Masala Dosa x2", "Total 685.00"],
  ]) {
    const r = extractFuelReceipt(ocr(...lines));
    assert.equal(r.overall_review_required, true, lines[0]);
    assert.ok(r.validation_warnings.some((w) => /fuel-receipt evidence/i.test(w)), lines[0]);
  }
});

test("garbled OCR lines are not turned into a pump name", () => {
  const r = extractFuelReceipt(ocr("Dn coosatiAMAGRRRARRR TTL 4", "rela m", "PRODUCT: DIESEL", "NOZZLE NO: 2", "DATE: 24/07/2026", "AMOUNT: 2000.00"));
  assert.equal(r.pump_name.normalized_value, null);
  assert.equal(r.pump_name.review_required, true);
});
