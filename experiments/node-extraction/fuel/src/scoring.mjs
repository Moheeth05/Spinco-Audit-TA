// Scoring for the three target fields ONLY (pump_name, transaction_date, total_amount) and the overall review outcome.
//
// Normalisation policy (documented, applied to prediction and label alike):
//   pump_name : upper-case; drop a leading "M/S"; keep letters and digits only (so "S V F F S" == "SVFFS"). A prediction is an
//               exact match iff its normalised string equals the normalised form of ANY acceptable form recorded for that receipt.
//               (Brand-only for a receipt that prints a dealer name is NOT exact; it is counted as `brand_only`.)
//   date      : exact string equality of the ISO normalised value.
//   amount    : equal to the cent (|a-b| < 0.005).
//
// Terms (per field, per group):
//   labelled     receipts whose label has a value for the field (a null label means "the correct output is nothing").
//   returned     the extractor produced a normalised value.
//   auto_usable  returned AND the field is not flagged review_required (i.e. it would be used without a person looking).
//   exact        returned and equal to the label.            exact_rate = exact / labelled   (abstaining is not credit)
//   precision    exact / returned.
//   false_confident  auto_usable AND not equal to the label (a wrong value marked fit for automatic use).
// A date label marked `ambiguous_on_receipt_alone` is one a careful reader must abstain on: returning a value is allowed only
// with recorded independent evidence; the report shows how many such dates were returned vs held for review.

const norm = (s) => String(s ?? "").toUpperCase().replace(/^\s*M\/S\b/, "").replace(/[^A-Z0-9]/g, "");

export function pumpVerdict(pred, label) {
  const forms = (label.acceptable_forms || []).map(norm).filter(Boolean);
  const p = norm(pred?.normalized_value);
  if (label.normalized_value === null || forms.length === 0) return { expected: null, returned: Boolean(p), exact: !p, brandOnly: false };
  if (!p) return { expected: forms[0], returned: false, exact: false, brandOnly: false };
  const exact = forms.includes(p);
  const brandOnly = !exact && /^(BHARATPETROLEUM(CORP)?|BPCL|HP|HPCL|HINDUSTANPETROLEUM|INDIANOIL|IOCL|IOC|NAYARA(ENERGY)?|JIOBP|SHELL)$/.test(p);
  return { expected: forms[0], returned: true, exact, brandOnly };
}

export function dateVerdict(pred, label) {
  const p = pred?.normalized_value ?? null;
  const t = label.normalized_value ?? null;
  return { returned: p !== null, exact: p !== null && p === t, expected: t };
}

export function amountVerdict(pred, label) {
  const p = pred?.normalized_value;
  const t = label.normalized_value;
  const returned = typeof p === "number" && Number.isFinite(p);
  if (t === null || t === undefined) return { returned, exact: !returned, expected: null };
  return { returned, exact: returned && Math.abs(p - t) < 0.005, expected: t };
}

const usable = (field) => field && field.review_required === false;

export function scoreReceipt(pred, rec) {
  const L = rec.fields;
  const pump = pumpVerdict(pred.pump_name, L.pump_name);
  const date = dateVerdict(pred.transaction_date, L.transaction_date);
  const amount = amountVerdict(pred.total_amount, L.total_amount);
  // A field whose label is `excluded` (assistant-read label withheld by policy, or illegible to the human reviewer) cannot be judged:
  // it is not labelled, not exact, and can never be called false-confident.
  const field = (v, f, labelled, excluded) => ({
    labelled: labelled && !excluded,
    unjudged: Boolean(excluded),
    returned: v.returned,
    exact: labelled && !excluded ? v.exact : null,
    autoUsable: v.returned && usable(f),
    falseConfident: !excluded && v.returned && usable(f) && !v.exact,
  });
  const out = {
    id: rec.receipt_id,
    pump_name: { ...field(pump, pred.pump_name, L.pump_name.normalized_value !== null, L.pump_name.excluded), brandOnly: pump.brandOnly, expected: pump.expected, got: pred.pump_name?.normalized_value ?? null },
    transaction_date: { ...field(date, pred.transaction_date, L.transaction_date.normalized_value !== null, L.transaction_date.excluded), ambiguousAlone: Boolean(L.transaction_date.ambiguous_on_receipt_alone), expected: date.expected, got: pred.transaction_date?.normalized_value ?? null },
    total_amount: { ...field(amount, pred.total_amount, L.total_amount.normalized_value !== null, L.total_amount.excluded), expected: amount.expected, got: pred.total_amount?.normalized_value ?? null },
    overallReviewRequired: Boolean(pred.overall_review_required),
    labelStatus: rec.annotation_status,
    unscored: [L.pump_name, L.transaction_date, L.total_amount].every((x) => x.excluded),
  };
  const fieldsWrong = [out.pump_name, out.transaction_date, out.total_amount].some((f) => f.labelled && !f.exact);
  out.anyFieldWrongOrMissing = fieldsWrong;
  if (rec.kind === "non_fuel") {
    // A non-fuel control has no correct fuel fields: the only correct outcome is to hold it for review.
    out.falseAccept = !out.overallReviewRequired;
    out.trueAccept = out.overallReviewRequired; // "true hold": correctly not accepted
    out.reviewJustified = out.overallReviewRequired ? true : null;
    out.anyFieldWrongOrMissing = true;
    return out;
  }
  out.falseAccept = !out.overallReviewRequired && fieldsWrong; // the whole record would be used automatically while at least one field is wrong
  out.reviewJustified = out.overallReviewRequired ? fieldsWrong || rec.expect_review === true || [L.pump_name, L.transaction_date].some((x) => x.review_required) : null;
  out.trueAccept = !out.overallReviewRequired && !fieldsWrong;
  return out;
}

const ratio = (n, d) => ({ n, d, rate: d ? n / d : null });

export function aggregate(allResults) {
  const results = allResults.filter((r) => !r.unscored); // receipts whose every label is excluded cannot be scored
  const agg = { receipts: results.length, unscored_labels_excluded: allResults.length - results.length, label_status: Object.fromEntries([...new Set(results.map((r) => r.labelStatus))].map((s) => [s, results.filter((r) => r.labelStatus === s).length])) };
  for (const f of ["pump_name", "transaction_date", "total_amount"]) {
    const rows = results.map((r) => r[f]);
    const labelled = rows.filter((x) => x.labelled);
    const returned = labelled.filter((x) => x.returned);
    const exact = labelled.filter((x) => x.exact);
    const autoUsable = rows.filter((x) => x.autoUsable);
    agg[f] = {
      labelled: labelled.length,
      coverage: ratio(returned.length, labelled.length),
      exact_rate: ratio(exact.length, labelled.length),
      precision_when_returned: ratio(exact.length, returned.length),
      auto_usable: ratio(autoUsable.length, rows.length),
      false_confident_per_auto_usable: ratio(rows.filter((x) => x.falseConfident).length, autoUsable.length),
      false_confident_per_receipt: ratio(rows.filter((x) => x.falseConfident).length, rows.length),
    };
    if (f === "pump_name") agg[f].brand_only_when_dealer_printed = ratio(labelled.filter((x) => x.brandOnly).length, labelled.length);
    if (f === "transaction_date") {
      const amb = rows.filter((x) => x.labelled && x.ambiguousAlone);
      const clear = rows.filter((x) => x.labelled && !x.ambiguousAlone);
      agg[f].resolvable_on_receipt_alone = { receipts: clear.length, exact: clear.filter((x) => x.exact).length, rate: clear.length ? clear.filter((x) => x.exact).length / clear.length : null };
      agg[f].ambiguous_on_receipt_alone = { receipts: amb.length, returned_a_date: amb.filter((x) => x.returned).length, held_for_review: amb.filter((x) => !x.autoUsable).length, wrong_and_confident: amb.filter((x) => x.falseConfident).length };
    }
  }
  const review = results.filter((r) => r.overallReviewRequired);
  agg.overall = {
    review_required_rate: ratio(review.length, results.length),
    review_justified: ratio(review.filter((r) => r.reviewJustified).length, review.length),
    true_accept: ratio(results.filter((r) => r.trueAccept).length, results.length),
    false_accept_rate: ratio(results.filter((r) => r.falseAccept).length, results.length),
    all_three_exact: ratio(results.filter((r) => !r.anyFieldWrongOrMissing).length, results.length),
  };
  return agg;
}
