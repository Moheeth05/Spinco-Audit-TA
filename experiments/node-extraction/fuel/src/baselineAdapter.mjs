// BASELINE: the EXISTING Node pipeline, unmodified (OCR text -> src/billParser.mjs parseBillText), mapped onto the
// three-field interface. Nothing here improves or special-cases anything; it only re-labels what the current parser returns.
//
// "Fit for automatic use" for the baseline = the parser's own status === "EXTRACTED" (that is what the application treats as
// not needing review). The baseline has no per-field review flags, so all three fields inherit the bill-level status.
import { parseBillText } from "../../src/billParser.mjs";

export function baselineExtract(ocr) {
  const parsed = parseBillText(ocr.text || "");
  const reviewRequired = parsed.status !== "EXTRACTED";
  const mk = (raw, norm, evidence) => ({
    raw_value: raw ?? null,
    normalized_value: norm ?? null,
    confidence: parsed.confidence,
    confidence_type: "baseline_parser_label_not_a_probability",
    evidence: evidence ? [evidence] : [],
    ambiguity: null,
    review_required: raw === null || raw === undefined ? true : reviewRequired,
  });
  const date = parsed.billDate ? parsed.billDate.iso : null;
  return {
    pump_name: mk(parsed.vendor, parsed.vendor, parsed.vendor ? "billParser.findVendor" : null),
    transaction_date: mk(date, date, date ? "billParser.findDate (day-first default for numeric dates)" : null),
    total_amount: mk(parsed.amount, parsed.amount, parsed.amount !== null ? "billParser.findAmount" : null),
    validation_warnings: parsed.warnings,
    overall_review_required: reviewRequired || parsed.vendor === null || date === null || parsed.amount === null,
    _implementation: "baseline: experiments/node-extraction/src/billParser.mjs (unchanged)",
  };
}
