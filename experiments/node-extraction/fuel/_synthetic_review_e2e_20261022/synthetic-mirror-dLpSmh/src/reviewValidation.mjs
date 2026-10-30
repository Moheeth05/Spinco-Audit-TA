// Validation of human-review submissions (the export written by the offline review tool, and overlay entries derived from it).
// Pure functions, no I/O. The status vocabularies below are exactly the ones the review tool (scripts/build_review_pack.mjs -> review.html)
// can emit; nothing is added. Messages name the receipt / field / rule only and NEVER echo a reviewer-supplied value.
//
// Reviewer identity: the reviewer id is SELF-ATTESTED text typed into the review tool. This module only checks that it is well formed and
// gives a stable identity key so that one person cannot be counted as two reviewers. It does not (and cannot) prove who the person is, and
// it does not consult label_policy.json `authorized_reviewers` (see verification/README.md, "What is NOT enforced").

export const REVIEWER_ID_MIN = 3;
export const REVIEWER_ID_MAX = 64;
// ASCII only (no look-alike characters); starts and ends with a letter/digit; interior may contain space . _ @ - ; "+" is reserved as the separator
// used when several reviewers are recorded on one label.
const REVIEWER_ID_RE = /^[A-Za-z0-9][A-Za-z0-9 ._@-]*[A-Za-z0-9]$/;

export const PUMP_STATUSES = ["read", "logo_only", "not_printed", "illegible"];
export const DATE_STATUSES = ["read", "illegible"];
export const AMOUNT_STATUSES = ["read", "illegible"];
// "order evidence" values offered by the review tool. Receipt-intrinsic evidence can support "order provable from this receipt alone".
export const INTRINSIC_ORDER_EVIDENCE = ["component_gt_12", "month_name_or_iso", "format_cue_on_receipt", "equal_components", "month_in_bill_number"];
export const NON_INTRINSIC_ORDER_EVIDENCE = ["other_documents", "none"];
export const DATE_YEAR_MIN = 1990, DATE_YEAR_MAX = 2100; // sanity bound only (catches e.g. "0026-09-24" typed into a date box); not derived from any clock
export const AMOUNT_MAX = 10_000_000;
// Source types (corpus/annotations.json `source_type`) that may receive human-verified labels: the receipts the review pack offers
// (build_review_pack.mjs) and that REPORT_PHASE2.md section 6 counts as awaiting verification (22 authentic + 1 suspected-template page);
// intake receipts are `authentic` by their enforced provenance. `synthetic` and `generic_control` records carry construction truth and can never
// be human-verified; any other or missing source type is NOT eligible (fail closed).
export const HUMAN_VERIFIABLE_SOURCE_TYPES = ["authentic", "suspected_template_generated"];
export const isHumanVerifiableSourceType = (t) => typeof t === "string" && HUMAN_VERIFIABLE_SOURCE_TYPES.includes(t);

/** Trim and collapse internal whitespace. Returns the normalised id, or null if the input is not a well-formed reviewer id. */
export function normalizeReviewerId(raw) {
  if (typeof raw !== "string") return null;
  const id = raw.trim().replace(/\s+/g, " ");
  if (id.length < REVIEWER_ID_MIN || id.length > REVIEWER_ID_MAX) return null;
  return REVIEWER_ID_RE.test(id) ? id : null;
}
/** Identity key for "is this the same reviewer": case-insensitive on the normalised id. Distinct spellings stay distinct people. */
export const reviewerKey = (normalizedId) => normalizedId.toLowerCase();

export function validateReviewerId(raw) {
  const id = normalizeReviewerId(raw);
  if (id) return { ok: true, id, key: reviewerKey(id) };
  return { ok: false, error: `reviewer_id must be ${REVIEWER_ID_MIN}-${REVIEWER_ID_MAX} characters of letters, digits, space . _ @ - (starting and ending with a letter or digit); it is self-attested text` };
}
/** Validate a stored (possibly composite "A+B") reviewer id, as written on a verified label. */
export function validateStoredReviewerId(raw) {
  if (typeof raw !== "string") return false;
  const parts = raw.split("+");
  return parts.every((p) => normalizeReviewerId(p) === p) && new Set(parts.map(reviewerKey)).size === parts.length;
}

/** Strict real-calendar-date check for YYYY-MM-DD (leap years included). No other format is accepted. */
export function isRealIsoDate(s) {
  if (typeof s !== "string" || !/^\d{4}-\d{2}-\d{2}$/.test(s)) return false;
  const y = Number(s.slice(0, 4)), m = Number(s.slice(5, 7)), d = Number(s.slice(8, 10));
  if (y < DATE_YEAR_MIN || y > DATE_YEAR_MAX || m < 1 || m > 12 || d < 1) return false;
  const leap = (y % 4 === 0 && y % 100 !== 0) || y % 400 === 0;
  const dim = [31, leap ? 29 : 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31][m - 1];
  return d <= dim;
}

const nonEmptyString = (v) => typeof v === "string" && v.trim().length > 0;
const absent = (v) => v === null || v === undefined || (typeof v === "string" && v.trim() === "");
const isObj = (v) => v && typeof v === "object" && !Array.isArray(v);

/**
 * Validate the three field objects of one reviewer entry (status vocabulary, value presence, value validity, status/value and
 * provability/evidence consistency). Returns an array of problems ("pump_name: ..."); empty = valid.
 */
export function validateReviewFields(e) {
  const errs = [];
  if (!isObj(e)) return ["entry is not an object"];
  if (e.notes != null && typeof e.notes !== "string") errs.push("notes must be text");
  // ---- pump_name
  const p = e.pump_name;
  if (!isObj(p)) errs.push("pump_name: missing");
  else if (!PUMP_STATUSES.includes(p.status)) errs.push(`pump_name: unsupported status (allowed: ${PUMP_STATUSES.join(", ")})`);
  else if (p.status === "read" || p.status === "logo_only") {
    if (!nonEmptyString(p.value)) errs.push(`pump_name: status '${p.status}' requires a non-empty value`);
    else if (p.value.length > 200) errs.push("pump_name: value is unreasonably long");
    if (p.other_acceptable_forms !== undefined && !(Array.isArray(p.other_acceptable_forms) && p.other_acceptable_forms.every((x) => typeof x === "string" && x.length <= 200))) errs.push("pump_name: other_acceptable_forms must be a list of short strings");
  } else if (!absent(p.value)) errs.push(`pump_name: status '${p.status}' contradicts a value being present (clear the value or choose 'read')`);
  if (isObj(p) && p.printed_text != null && typeof p.printed_text !== "string") errs.push("pump_name: printed_text must be text");
  // ---- transaction_date
  const d = e.transaction_date;
  if (!isObj(d)) errs.push("transaction_date: missing");
  else if (!DATE_STATUSES.includes(d.status)) errs.push(`transaction_date: unsupported status (allowed: ${DATE_STATUSES.join(", ")})`);
  else if (d.status === "read") {
    if (!isRealIsoDate(d.iso_date)) errs.push(`transaction_date: iso_date must be a real calendar date written YYYY-MM-DD (years ${DATE_YEAR_MIN}-${DATE_YEAR_MAX})`);
    if (typeof d.order_provable_from_receipt !== "boolean") errs.push("transaction_date: order_provable_from_receipt must be true or false");
    const ev = d.order_evidence;
    const known = [...INTRINSIC_ORDER_EVIDENCE, ...NON_INTRINSIC_ORDER_EVIDENCE];
    if (!absent(ev) && !known.includes(ev)) errs.push("transaction_date: unsupported order_evidence");
    else if (typeof d.order_provable_from_receipt === "boolean") {
      if (d.order_provable_from_receipt === true && NON_INTRINSIC_ORDER_EVIDENCE.includes(ev)) errs.push(`transaction_date: order marked provable from the receipt but evidence is '${ev}'`);
      if (d.order_provable_from_receipt === false && INTRINSIC_ORDER_EVIDENCE.includes(ev)) errs.push(`transaction_date: order marked NOT provable but receipt-intrinsic evidence '${ev}' is given`);
    }
    if (isRealIsoDate(d.iso_date)) {
      const day = Number(d.iso_date.slice(8, 10)), month = Number(d.iso_date.slice(5, 7));
      if (d.order_evidence === "component_gt_12" && !(day > 12)) errs.push("transaction_date: evidence 'component_gt_12' but the day is not greater than 12");
      if (d.order_evidence === "equal_components" && day !== month) errs.push("transaction_date: evidence 'equal_components' but day and month differ");
    }
  } else if (!absent(d.iso_date)) errs.push("transaction_date: status 'illegible' contradicts a date being present (clear the date or choose 'read')");
  if (isObj(d) && d.printed_text != null && typeof d.printed_text !== "string") errs.push("transaction_date: printed_text must be text");
  // ---- total_amount
  const a = e.total_amount;
  if (!isObj(a)) errs.push("total_amount: missing");
  else if (!AMOUNT_STATUSES.includes(a.status)) errs.push(`total_amount: unsupported status (allowed: ${AMOUNT_STATUSES.join(", ")})`);
  else if (a.status === "read") {
    const v = a.value;
    if (typeof v !== "number" || !Number.isFinite(v)) errs.push("total_amount: status 'read' requires a numeric value");
    else if (v <= 0 || v > AMOUNT_MAX) errs.push(`total_amount: value must be greater than 0 and at most ${AMOUNT_MAX}`);
    else if (Math.abs(v * 100 - Math.round(v * 100)) > 1e-6) errs.push("total_amount: value has more than two decimal places");
  } else if (!absent(a.value)) errs.push("total_amount: status 'illegible' contradicts a value being present (clear the value or choose 'read')");
  if (isObj(a) && a.printed_text != null && typeof a.printed_text !== "string") errs.push("total_amount: printed_text must be text");
  return errs;
}

/**
 * The comparison key used to decide whether two reviewers AGREE. It covers every required field's status AND value, and the date's
 * provability claim, so agreement on a subset of fields can never hide disagreement on another. Assumes validateReviewFields passed.
 */
export function agreementKey(e) {
  const fold = (s) => String(s ?? "").toUpperCase().replace(/[^A-Z0-9]/g, "");
  return {
    pump_name: { status: e.pump_name.status, value: fold(e.pump_name.value) },
    transaction_date: { status: e.transaction_date.status, value: e.transaction_date.iso_date ?? null, order_provable_from_receipt: e.transaction_date.status === "read" ? e.transaction_date.order_provable_from_receipt : null },
    total_amount: { status: e.total_amount.status, value: e.total_amount.value ?? null },
  };
}
/**
 * Everything the review tool does at "Export JSON", as a pure function (the page embeds this module's source, so the UI and
 * apply_verification.mjs enforce literally the same rules). A receipt whose three statuses are not all chosen is "incomplete" (left out of the
 * export, as before); a complete receipt that fails validation BLOCKS the whole export. Messages never contain receipt values.
 * Returns { ok:false, errors, incomplete } or { ok:true, payload, incomplete }.
 */
export function buildExportPayload({ items, state, reviewerId, authorizationConfirmed, packId, blind, exportedAt }) {
  const errors = [], incomplete = [], records = {};
  const rid = validateReviewerId(reviewerId);
  if (!rid.ok) errors.push(rid.error);
  if (authorizationConfirmed !== true) errors.push("tick the authorization box (only if you are authorized by the company to view these receipts)");
  for (const it of items) {
    const r = state && state[it.id];
    const complete = r && r.pump_name && r.pump_name.status && r.transaction_date && r.transaction_date.status && r.total_amount && r.total_amount.status;
    if (!complete) { incomplete.push(it.id); continue; }
    const problems = validateReviewFields(r);
    if (problems.length) for (const p of problems) errors.push(`receipt ${it.id}: ${p}`);
    else records[it.id] = r;
  }
  if (!errors.length && !Object.keys(records).length) errors.push("no complete receipt to export (choose a status for all three fields of at least one receipt)");
  if (errors.length) return { ok: false, errors, incomplete };
  return { ok: true, incomplete, payload: { schema: "fuel-review-export/v1", pack_id: packId, reviewer_id: rid.id, authorization_confirmed: true, exported_at: exportedAt, blind, records } };
}
export const fieldsInDisagreement =(x, y) => ["pump_name", "transaction_date", "total_amount"].filter((f) => JSON.stringify(agreementKey(x)[f]) !== JSON.stringify(agreementKey(y)[f]));
