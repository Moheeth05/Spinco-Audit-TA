// Merges one or more reviewers' exports into corpus/verified_labels.json. Usage:
//   node scripts/apply_verification.mjs <export1.json> [<export2.json> ...] [--out <verified_labels.json>] [--annotations-dir <fuelDir>]
// Rules (nothing here can create verification out of nothing):
//   * every export is validated BEFORE anything is written; one invalid export or entry rejects the whole invocation (exit 1, nothing written):
//       - schema fuel-review-export/v1, a well-formed reviewer_id (self-attested text, see src/reviewValidation.mjs), authorization_confirmed === true,
//       - every receipt entry: supported statuses only, status/value consistency, real YYYY-MM-DD calendar date, date provability consistent with its evidence;
//   * the same reviewer (compared case-insensitively on the normalised id) may appear only once per invocation - one person is never "two reviewers";
//   * with two or more exports, a receipt is verified only if the reviewers agree on EVERY required field: status and value of pump_name,
//     transaction_date and total_amount, and whether the date's day/month order is provable from the receipt; otherwise it is appended to
//     corpus/adjudication_needed.json and is NOT marked verified;
//   * an existing verified record is NEVER rewritten: a later review that agrees is a no-op; one that disagrees is recorded as an OPEN conflict in
//     corpus/review_conflicts.json (exit 3), and the loader then holds that receipt back from `human_verified` until a person resolves it;
//   * adjudication, conflicts and an audit history (corpus/review_history.json) are append-only - this script never deletes or replaces earlier entries;
//   * only receipts whose source type (from corpus/annotations.json, or the intake register's provenance - never from an id's spelling or the export)
//     is human-verifiable may be named: authentic and suspected_template_generated. Synthetic, generic_control, unknown or missing types reject the invocation;
//   * the output records who verified what and when, and field-level agreement with the provisional labels.
// NOT enforced: who the reviewer really is. reviewer_id is typed into the review tool; label_policy.json `authorized_reviewers` is not consulted.
import crypto from "node:crypto";
import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { FIELDS, applyReviewerEntry, verificationEligibility } from "../src/labels.mjs";
import { validateReviewerId, validateReviewFields, fieldsInDisagreement } from "../src/reviewValidation.mjs";

const FUEL0 = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const argv = process.argv.slice(2);
const files = [], opt = {};
for (let i = 0; i < argv.length; i++) argv[i].startsWith("--") ? (opt[argv[i].slice(2)] = argv[++i]) : files.push(argv[i]);
const FUEL = opt["annotations-dir"] ? path.resolve(opt["annotations-dir"]) : FUEL0;
if (!files.length) { console.error("usage: apply_verification.mjs <export.json> ..."); process.exit(2); }

const readJson = (f) => JSON.parse(fs.readFileSync(f, "utf8"));
const writeJson = (f, obj) => { const tmp = `${f}.tmp-${process.pid}`; fs.writeFileSync(tmp, JSON.stringify(obj, null, 1)); fs.renameSync(tmp, f); }; // never leaves a half-written file
const outFile = path.resolve(opt.out || path.join(FUEL, "corpus", "verified_labels.json"));
const outDir = path.dirname(outFile);
const NOW = new Date().toISOString();

// ---- 1. read + validate everything first (nothing is written until all of it passes) ------------------------------------------------
const errors = [];
const exports_ = [];
for (const f of files) {
  const name = path.basename(f);
  let bytes, data;
  try { bytes = fs.readFileSync(f); data = JSON.parse(bytes.toString("utf8")); } catch { errors.push(`${name}: cannot be read as JSON`); continue; }
  if (!data || data.schema !== "fuel-review-export/v1") { errors.push(`${name}: not a fuel-review-export/v1 file`); continue; }
  const rid = validateReviewerId(data.reviewer_id);
  if (!rid.ok) errors.push(`${name}: ${rid.error} - rejected`);
  if (data.authorization_confirmed !== true) errors.push(`${name}: authorization_confirmed is not true - rejected`);
  if (data.exported_at != null && (typeof data.exported_at !== "string" || Number.isNaN(Date.parse(data.exported_at)))) errors.push(`${name}: exported_at is not a timestamp`);
  if (!data.records || typeof data.records !== "object" || Array.isArray(data.records)) { errors.push(`${name}: records must be an object keyed by receipt id`); continue; }
  for (const [id, e] of Object.entries(data.records)) for (const p of validateReviewFields(e)) errors.push(`${name}: receipt ${id}: ${p}`);
  exports_.push({ file: f, name, data, rid, sha256: crypto.createHash("sha256").update(bytes).digest("hex") });
}
const seen = new Map();
for (const x of exports_) {
  if (!x.rid.ok) continue;
  if (seen.has(x.rid.key)) errors.push(`${x.name}: duplicate reviewer identity '${x.rid.id}' (also in ${seen.get(x.rid.key)}): the same reviewer is not an independent second reviewer - rejected`);
  else seen.set(x.rid.key, x.name);
}
if (errors.length) { console.error("REJECTED - nothing was written:\n  " + errors.join("\n  ")); process.exit(1); }

const annotationRecords = readJson(path.join(FUEL, "corpus", "annotations.json")).records;
const annotations = Object.fromEntries(annotationRecords.map((r) => [r.receipt_id, r]));
const regFile = path.join(FUEL, "intake", "intake_register.json");
const intake = fs.existsSync(regFile) ? readJson(regFile).receipts || [] : [];
const known = new Set([...Object.keys(annotations), ...intake.map((x) => x.receipt_id)]);

// Source-type eligibility (fail closed): only receipts the corpus files classify as human-verifiable may be named in an export. A synthetic or
// control record (construction truth), or one with an unknown/missing source type, rejects the whole invocation before anything is written.
const elig = verificationEligibility(annotationRecords, intake);
const ineligible = [];
for (const x of exports_) for (const id of Object.keys(x.data.records)) if (known.has(id) && !elig.eligible.has(id)) ineligible.push(`${x.name}: receipt ${id}: ${elig.blocked.get(id) || "not eligible for human verification"}`);
if (ineligible.length) { console.error("REJECTED - nothing was written:\n  " + ineligible.join("\n  ")); process.exit(1); }

// ---- 2. group per receipt; reviewers must agree on every required field ---------------------------------------------------------------
const byReceipt = {}, ignoredUnknown = [];
for (const x of exports_) for (const [id, e] of Object.entries(x.data.records)) {
  if (!known.has(id)) { console.warn(`ignored unknown receipt id ${id}`); ignoredUnknown.push(id); continue; }
  (byReceipt[id] ||= []).push({ reviewer_id: x.rid.id, exported_at: x.data.exported_at, entry: { pump_name: e.pump_name, transaction_date: e.transaction_date, total_amount: e.total_amount, notes: e.notes ?? null } });
}
const candidates = {}, disagreements = {};
for (const [id, list] of Object.entries(byReceipt)) {
  const first = list[0];
  const diff = [...new Set(list.slice(1).flatMap((x) => fieldsInDisagreement(first.entry, x.entry)))];
  if (diff.length) { disagreements[id] = { list, fields: FIELDS.filter((f) => diff.includes(f)) }; continue; }
  candidates[id] = {
    reviewer_id: list.map((x) => x.reviewer_id).join("+"), authorization_confirmed: true, identity_assurance: "self_attested", reviewer_count: list.length,
    verified_at: first.exported_at || NOW, ...first.entry, second_reviewer_agreed: list.length > 1,
  };
}

// ---- 3. never overwrite an existing verified record ------------------------------------------------------------------------------------
const outDoc = fs.existsSync(outFile) ? readJson(outFile) : {};
const existing = outDoc.records || {};
const newVerified = {}, unchanged = [], conflicts = [];
const existingIsComparable = (ex) => validateReviewFields(ex).length === 0;
for (const [id, cand] of Object.entries(candidates)) {
  const ex = existing[id];
  if (!ex) { newVerified[id] = cand; continue; }
  const fields = existingIsComparable(ex) ? fieldsInDisagreement(ex, cand) : [...FIELDS]; // an unreadable existing record cannot be shown consistent
  if (!fields.length) unchanged.push(id);
  else conflicts.push({ receipt_id: id, kind: "later_review_conflicts_with_existing_verified_label", fields, ex, incoming: byReceipt[id] });
}
for (const [id, d] of Object.entries(disagreements)) if (existing[id]) conflicts.push({ receipt_id: id, kind: "reviewers_disagree_on_already_verified_label", fields: d.fields, ex: existing[id], incoming: d.list });

const agreementStats = Object.fromEntries(FIELDS.map((f) => [f, { agree: 0, disagree: 0, illegible: 0 }]));
for (const [id, v] of Object.entries(newVerified)) {
  if (!annotations[id]) continue;
  const merged = applyReviewerEntry(annotations[id], v);
  for (const f of FIELDS) { const a = merged.verification.agreement_with_provisional[f]; agreementStats[f][a === "illegible" ? "illegible" : a ? "agree" : "disagree"]++; }
}

// ---- 4. write (append-only side files; the verified file only gains records) ----------------------------------------------------------
fs.mkdirSync(outDir, { recursive: true });
if (Object.keys(newVerified).length) writeJson(outFile, { ...outDoc, generated: NOW, note: "Written by apply_verification.mjs from reviewer exports ONLY. Reviewer ids are self-attested.", records: { ...existing, ...newVerified } });

const adjFile = path.join(outDir, "adjudication_needed.json");
if (Object.keys(disagreements).length) {
  const adj = fs.existsSync(adjFile) ? readJson(adjFile) : {};
  for (const [id, d] of Object.entries(disagreements)) {
    adj[id] ||= [];
    for (const x of d.list) {
      const item = { reviewer_id: x.reviewer_id, entry: x.entry };
      if (!adj[id].some((y) => y.reviewer_id === item.reviewer_id && JSON.stringify(y.entry) === JSON.stringify(item.entry))) adj[id].push({ ...item, recorded_at: NOW, fields_in_disagreement: d.fields });
    }
  }
  writeJson(adjFile, adj);
}

const cfFile = path.join(outDir, "review_conflicts.json");
if (conflicts.length) {
  const cf = fs.existsSync(cfFile) ? readJson(cfFile) : { schema: "fuel-review-conflicts/v1", note: "Append-only. An OPEN event holds the receipt back from human_verified (src/labels.mjs). This script never resolves or deletes events; a person must adjudicate.", events: [] };
  for (const c of conflicts) {
    const incomingIds = c.incoming.map((x) => x.reviewer_id).join("+");
    if (cf.events.some((e) => e.status === "open" && e.receipt_id === c.receipt_id && e.incoming_reviewers === incomingIds && JSON.stringify(e.fields_in_conflict) === JSON.stringify(c.fields))) continue;
    cf.events.push({ status: "open", detected_at: NOW, receipt_id: c.receipt_id, kind: c.kind, fields_in_conflict: c.fields, existing_reviewer_id: c.ex.reviewer_id, existing_entry: c.ex, incoming_reviewers: incomingIds, incoming: c.incoming.map((x) => ({ reviewer_id: x.reviewer_id, entry: x.entry })) });
  }
  writeJson(cfFile, cf);
}

const histFile = path.join(outDir, "review_history.json");
const hist = fs.existsSync(histFile) ? readJson(histFile) : { schema: "fuel-review-history/v1", note: "Append-only audit trail of apply_verification.mjs invocations.", events: [] };
hist.events.push({
  applied_at: NOW, reviewer_identity_assurance: "self_attested", authorized_reviewers_enforced: false,
  exports: exports_.map((x) => ({ file: x.name, reviewer_id: x.rid.id, exported_at: x.data.exported_at ?? null, sha256: x.sha256, receipts: Object.keys(x.data.records).length })),
  verified_new: Object.keys(newVerified), unchanged_matches_existing: unchanged, adjudication_needed: Object.keys(disagreements), conflicts: conflicts.map((c) => c.receipt_id), ignored_unknown_ids: ignoredUnknown,
});
writeJson(histFile, hist);

console.log(JSON.stringify({
  verified_receipts_now: Object.keys(newVerified).length, unchanged_already_verified: unchanged.length, needing_adjudication: Object.keys(disagreements).length, open_conflicts_recorded: conflicts.length,
  reviewer_identity_assurance: "self_attested", authorized_reviewers_enforced: false,
  agreement_with_provisional_labels: agreementStats, written: outFile,
}, null, 1));
if (conflicts.length) {
  console.error(`CONFLICT: ${conflicts.length} receipt(s) already have a verified label that this review does not agree with (${conflicts.map((c) => c.receipt_id).join(", ")}). Nothing was overwritten; the conflict is recorded in ${cfFile} and the receipt is held back from human_verified until a person adjudicates it.`);
  process.exitCode = 3;
}
