// Evidence for the receipt-family-specific "month printed in the bill number" rule (see BILL_NUMBER_MONTH_SIGNATURE in src/fuelExtractor.mjs).
// For every authentic receipt: does OCR read the "<Mon>-<digits>-ORGNL" signature, and does its month equal the month of the printed date?
//   (a) OCR-only check - needs NO label: only receipts whose OCR'd date is unambiguous (day > 12) can be checked this way.
//   (b) check against the PROVISIONAL (assistant-read) label date - weaker, because the labels are not yet human-verified.
// Writes reports/bill_number_month_rule_evidence.{json,md}. Run on authentic receipts only; synthetic receipts are not evidence.
import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { CachedOCR } from "../src/ocr.mjs";
import { extractFuelReceipt } from "../src/fuelExtractor.mjs";

const FUEL = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const SIG = /\b(JAN(?:UARY)?|FEB(?:RUARY)?|MAR(?:CH)?|APR(?:IL)?|MAY|JUNE?|JULY?|AUG(?:UST)?|SEP(?:T(?:EMBER)?)?|OCT(?:OBER)?|NOV(?:EMBER)?|DEC(?:EMBER)?)\s*-\s*\d{3,}\s*-\s*ORGNL\b/i;
const MON = { JAN: 1, FEB: 2, MAR: 3, APR: 4, MAY: 5, JUN: 6, JUL: 7, AUG: 8, SEP: 9, OCT: 10, NOV: 11, DEC: 12 };
const recs = JSON.parse(fs.readFileSync(path.join(FUEL, "corpus", "annotations.json"), "utf8")).records.filter((r) => r.source_type === "authentic");
const ocr = new CachedOCR(path.join(FUEL, "corpus", "ocr-cache"));
const rows = [];
for (const r of recs) {
  const o = await ocr.read(path.join(FUEL, "corpus", "authentic", "images", `${r.receipt_id}.png`), { mode: "retry" });
  const m = o.text.match(SIG);
  const tokMonth = m ? MON[m[1].slice(0, 3).toUpperCase()] : null;
  const pred = extractFuelReceipt(o);
  const labelISO = r.fields.transaction_date.normalized_value;
  const labelMonth = labelISO ? Number(labelISO.slice(5, 7)) : null;
  // OCR-only: the date the extractor read WITHOUT using the rule, when it was unambiguous
  const readDate = pred.transaction_date.basis === "month_stated_in_bill_number" ? null : pred.transaction_date.normalized_value;
  rows.push({
    id: r.receipt_id, family: r.receipt_family, signature_read_by_ocr: m ? m[0].replace(/\s+/g, "") : null, token_month: tokMonth,
    ocr_unambiguous_date: readDate, ocr_unambiguous_month: readDate ? Number(readDate.slice(5, 7)) : null,
    label_date_provisional: labelISO, label_month: labelMonth, date_ambiguous_on_receipt_alone: Boolean(r.fields.transaction_date.ambiguous_on_receipt_alone),
    rule_used_by_extractor: pred.transaction_date.basis === "month_stated_in_bill_number",
  });
}
await ocr.close();
const withTok = rows.filter((x) => x.token_month);
const ocrOnly = withTok.filter((x) => x.ocr_unambiguous_month);
const vsLabel = withTok.filter((x) => x.label_month);
const out = {
  generated: new Date().toISOString(),
  caveat: "n is small, all receipts come from three reports (ER-39832, ER-40706, ER-40513) and a few printers; the labels used in check (b) are provisional. This is evidence that the convention holds in THESE examples, not proof that it holds generally.",
  authentic_receipts: rows.length,
  signature_read_by_ocr: withTok.length,
  ocr_only_check: { checkable: ocrOnly.length, agree: ocrOnly.filter((x) => x.token_month === x.ocr_unambiguous_month).length, disagree: ocrOnly.filter((x) => x.token_month !== x.ocr_unambiguous_month).map((x) => x.id) },
  label_check_provisional: { checkable: vsLabel.length, agree: vsLabel.filter((x) => x.token_month === x.label_month).length, disagree: vsLabel.filter((x) => x.token_month !== x.label_month).map((x) => x.id) },
  rule_used_by_extractor: rows.filter((x) => x.rule_used_by_extractor).map((x) => x.id),
  family_receipts_without_signature_read: rows.filter((x) => x.family === "orgnl_physical_receipt" && !x.signature_read_by_ocr).map((x) => x.id),
  rows,
};
fs.writeFileSync(path.join(FUEL, "reports", "bill_number_month_rule_evidence.json"), JSON.stringify(out, null, 1));
const md = [
  "# Evidence for the receipt-family-specific bill-number month rule (generated)\n",
  out.caveat + "\n",
  `* authentic receipts examined: ${out.authentic_receipts}; OCR read the \`<Mon>-<digits>-ORGNL\` signature on ${out.signature_read_by_ocr}.`,
  `* **(a) OCR-only check** (receipts whose OCR'd date is unambiguous, so no label is involved): ${out.ocr_only_check.agree}/${out.ocr_only_check.checkable} agree${out.ocr_only_check.disagree.length ? "; DISAGREE: " + out.ocr_only_check.disagree.join(", ") : ""}.`,
  `* **(b) against provisional labels**: ${out.label_check_provisional.agree}/${out.label_check_provisional.checkable} agree${out.label_check_provisional.disagree.length ? "; DISAGREE: " + out.label_check_provisional.disagree.join(", ") : ""}.`,
  `* receipts where the extractor actually used the rule to resolve an otherwise ambiguous date: ${out.rule_used_by_extractor.join(", ") || "none"}.`,
  `* family (\`orgnl_physical_receipt\`) receipts where OCR did not read the signature (rule silent; an ambiguous date stays unresolved): ${out.family_receipts_without_signature_read.join(", ") || "none"}.\n`,
  "| receipt | family | signature OCR'd | token month | OCR date (unambiguous) | provisional label date | rule used |", "|---|---|---|---|---|---|---|",
  ...rows.filter((x) => x.signature_read_by_ocr || x.family === "orgnl_physical_receipt").map((x) => `| ${x.id} | ${x.family} | ${x.signature_read_by_ocr ?? "-"} | ${x.token_month ?? "-"} | ${x.ocr_unambiguous_date ?? "-"} | ${x.label_date_provisional ?? "-"} | ${x.rule_used_by_extractor} |`),
  "\n**Scope of the rule:** only the exact signature (a month abbreviation, 3+ digits, the literal `ORGNL`, on a Bill/INV/Invoice/Receipt line). It only selects between day/month readings already present in a printed numeric date; it never supplies a missing day, month or year, never overrides an unambiguous date, and is silent (date stays unresolved, review required) when the month agrees with neither or both readings. It is not a general date rule and is not applied to any other receipt family.\n",
];
fs.writeFileSync(path.join(FUEL, "reports", "bill_number_month_rule_evidence.md"), md.join("\n"));
console.log(md.slice(0, 8).join("\n"));
