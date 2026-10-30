// Label loading with an explicit PROVENANCE / VERIFICATION layer.
//
//   corpus/annotations.json        provisional labels (authentic: read by the assistant from the page image; synthetic: construction truth)
//   corpus/verified_labels.json    OPTIONAL overlay written ONLY by scripts/apply_verification.mjs from an authorized human reviewer's export
//   verification/label_policy.json whether assistant-read labels may be used at all (company policy on AI review of the receipts)
//
// A record is "human_verified" only if the overlay has an entry for it with reviewer_id set AND authorization_confirmed === true.
// Nothing else - no script, test or report - may mark a label verified. If the policy says "excluded", every assistant-read authentic label
// is marked `excluded` (kept out of the scores) until a human has relabelled it.
import fs from "node:fs";
import path from "node:path";
import { validateReviewFields, validateStoredReviewerId, isHumanVerifiableSourceType } from "./reviewValidation.mjs";
import { inspectIntakeEntries, readIntakeRegister } from "./intakeRegister.mjs";

export const FIELDS = ["pump_name", "transaction_date", "total_amount"];

export function readPolicy(fuelDir) {
  const f = path.join(fuelDir, "verification", "label_policy.json");
  return fs.existsSync(f) ? JSON.parse(fs.readFileSync(f, "utf8")) : { assistant_read_labels: "provisional_included" };
}

const isAssistantRead = (r) => r.annotation_status === "provisional_assistant_read";

// An overlay entry is trusted only if it has a well-formed (self-attested) reviewer id, an authorization confirmation, a timestamp, AND field
// content that passes the same validation the export had to pass (status vocabulary, status/value consistency, real calendar date, ...).
export function overlayEntryIsValid(e) {
  return Boolean(e && validateStoredReviewerId(e.reviewer_id) && e.authorization_confirmed === true && e.verified_at && validateReviewFields(e).length === 0);
}

// Which receipt ids may receive a human-verified label. Classification comes ONLY from the trusted corpus files (annotations.json `source_type`,
// the intake register's enforced provenance) - never from a receipt id's spelling and never from anything a reviewer or an export supplies.
// Fail closed: synthetic / generic_control / unknown / missing source types are blocked, and an id that is blocked by either file stays blocked.
export function verificationEligibility(annotationRecords, intakeEntries = []) {
  const eligible = new Set(), blocked = new Map();
  for (const r of annotationRecords) {
    if (isHumanVerifiableSourceType(r.source_type)) eligible.add(r.receipt_id);
    else blocked.set(r.receipt_id, `source type '${typeof r.source_type === "string" ? r.source_type.slice(0, 40) : "(missing)"}' is not eligible for human verification`);
  }
  // Register entries (src/intakeRegister.mjs): usable only if structurally valid, provably an authentic issued receipt, unique in the register and not colliding
  // with an annotation id. Every id with a problem is blocked (so an export naming it is refused); a colliding annotation id is blocked too, because the id is ambiguous.
  const intake = inspectIntakeEntries(Array.isArray(intakeEntries) ? intakeEntries : [], new Set(annotationRecords.map((r) => r.receipt_id)));
  for (const e of intake.usable) eligible.add(e.receipt_id);
  for (const [id, reason] of intake.blocked) blocked.set(id, reason);
  for (const id of blocked.keys()) eligible.delete(id);
  return { eligible, blocked, intake };
}

// Apply one reviewer entry to one record (pure function; used by the loader and by the tests).
export function applyReviewerEntry(rec, e) {
  if (!isHumanVerifiableSourceType(rec.source_type)) throw new Error(`reviewer entry not applied: source type of ${rec.receipt_id} is not eligible for human verification`);
  const problems = validateReviewFields(e);
  if (problems.length) throw new Error(`invalid reviewer entry (not applied): ${problems.join("; ")}`);
  const out = structuredClone(rec);
  out.provisional_label = Object.fromEntries(FIELDS.map((f) => [f, structuredClone(rec.fields[f])]));
  const agreement = {};

  const p = e.pump_name || {};
  const pf = out.fields.pump_name;
  if (p.status === "illegible") { pf.excluded = true; }
  else if (p.status === "not_printed") { pf.normalized_value = null; pf.raw_value = null; pf.acceptable_forms = []; pf.excluded = false; }
  else if (p.status === "read" || p.status === "logo_only") {
    pf.raw_value = p.printed_text ?? p.value; pf.normalized_value = p.value; pf.acceptable_forms = [p.value, ...(p.other_acceptable_forms || [])].filter(Boolean);
    pf.evidence_source = p.status === "logo_only" ? "logo_wordmark" : "printed_text"; pf.excluded = false;
  }
  const d = e.transaction_date || {};
  const df = out.fields.transaction_date;
  if (d.status === "illegible") { df.excluded = true; }
  else if (d.status === "read") {
    df.raw_value = d.printed_text ?? null; df.normalized_value = d.iso_date ?? null; df.excluded = false;
    df.ambiguous_on_receipt_alone = d.order_provable_from_receipt === false; // the reviewer says nothing on the receipt proves day/month order
    df.order_evidence = d.order_evidence || null;
  }
  const a = e.total_amount || {};
  const af = out.fields.total_amount;
  if (a.status === "illegible") { af.excluded = true; }
  else if (a.status === "read") { af.raw_value = a.printed_text ?? null; af.normalized_value = typeof a.value === "number" ? a.value : null; af.excluded = false; }

  const norm = (s) => String(s ?? "").toUpperCase().replace(/[^A-Z0-9]/g, "");
  const before = out.provisional_label;
  agreement.pump_name = p.status === "illegible" ? "illegible" : (before.pump_name.acceptable_forms || []).map(norm).includes(norm(p.value)) || (p.status === "not_printed" && before.pump_name.normalized_value === null);
  agreement.transaction_date = d.status === "illegible" ? "illegible" : (before.transaction_date.normalized_value ?? null) === (d.iso_date ?? null);
  agreement.total_amount = a.status === "illegible" ? "illegible" : Math.abs((before.total_amount.normalized_value ?? NaN) - (a.value ?? NaN)) < 0.005;
  out.annotation_status = "human_verified";
  out.label_policy_status = "verified_by_authorized_human";
  out.manually_reviewed_by_independent_human = true;
  out.verification = { reviewer_id: e.reviewer_id, verified_at: e.verified_at, authorization_confirmed: true, agreement_with_provisional: agreement, notes: e.notes || null };
  return out;
}

export function loadAnnotations(fuelDir, { labels } = {}) {
  const all = JSON.parse(fs.readFileSync(path.join(fuelDir, "corpus", "annotations.json"), "utf8")).records;
  const vf = path.join(fuelDir, "corpus", "verified_labels.json");
  const overlay = fs.existsSync(vf) ? JSON.parse(fs.readFileSync(vf, "utf8")).records || {} : {};
  // A receipt with an OPEN review conflict (written by apply_verification.mjs when a later review disagrees with an already-verified label)
  // is held back: its overlay entry is not applied, so it is not human_verified until a person resolves the conflict. Nothing is deleted.
  const cf = path.join(fuelDir, "corpus", "review_conflicts.json");
  const openConflicts = new Set(fs.existsSync(cf) ? (JSON.parse(fs.readFileSync(cf, "utf8")).events || []).filter((x) => x.status === "open").map((x) => x.receipt_id) : []);
  // Newly intaken authentic receipts: they carry NO assistant label at all. They enter as records whose every field is `excluded` until an
  // authorized human has verified them (overlay), so they can be run through the extractor but are never scored on a guess.
  const reg = readIntakeRegister(fuelDir);
  const elig = verificationEligibility(all, reg.entries); // computed from the annotation records BEFORE intake records are appended below
  // An invalid register (unreadable, not an object, `receipts` missing / not a list) is never trusted: no intake record is created, loudly, and no error is thrown.
  // Only entries the register proves to be usable (src/intakeRegister.mjs) become records. Every other entry is SKIPPED, announced and counted - it can never be
  // scored, run or verified.
  if (reg.status === "invalid") console.warn(`intake register ignored by the label loader: ${reg.problem}; no intake receipt is loaded or verifiable until it is repaired`);
  const skippedIntake = elig.intake.rejected.map((x) => ({ receipt_id: x.receipt_id ?? "(no usable receipt_id)", reason: x.reason }));
  for (const x of skippedIntake) console.warn(`intake entry ${x.receipt_id}: skipped by the label loader (${x.reason}); it cannot be scored, run or human-verified`);
  for (const e of elig.intake.usable) {
    const blank = { raw_value: null, normalized_value: null, confidence: null, evidence: [], review_required: false, excluded: true };
    const description = typeof e.provenance.source_description === "string" ? e.provenance.source_description : "(no description recorded)";
    all.push({
      receipt_id: e.receipt_id, source_type: "authentic", source_reference: `intake: ${description}`, receipt_family: e.receipt_family,
      split: e.split === "heldout_untouched" ? "intake_heldout" : "intake_dev", brand_family: e.brand_family, difficulty: [], kind: "fuel", image_path: e.image_path,
      fields: { pump_name: { ...blank, acceptable_forms: [] }, transaction_date: { ...blank, ambiguous_on_receipt_alone: false }, total_amount: { ...blank } },
      annotation_status: "pending_human_label", label_policy_status: "pending_authorized_human_verification", manually_reviewed_by_independent_human: false, notes: [],
    });
  }
  const policy = readPolicy(fuelDir);
  const mode = labels || (policy.assistant_read_labels === "excluded" ? "verified_only" : "all");
  let verified = 0, excluded = 0, provisional = 0, invalidOverlay = 0, heldByConflict = 0, ineligibleOverlay = 0;
  const records = all.map((rec) => {
    let r = rec;
    const e = overlay[rec.receipt_id];
    if (e) {
      if (!elig.eligible.has(rec.receipt_id)) ineligibleOverlay++; // synthetic / control / unknown source type: construction truth is kept, the overlay is ignored
      else if (openConflicts.has(rec.receipt_id)) heldByConflict++;
      else if (overlayEntryIsValid(e)) { r = applyReviewerEntry(rec, e); verified++; }
      else invalidOverlay++; // an entry without reviewer id / authorization is ignored, never trusted
    }
    if (r.annotation_status === "provisional_assistant_read") {
      if (mode === "verified_only") { r = structuredClone(r); for (const f of FIELDS) r.fields[f].excluded = true; r.annotation_status = "excluded_pending_human_label"; excluded++; }
      else provisional++;
    }
    return r;
  });
  return { records, eligibility: elig, summary: { mode, policy, human_verified: verified, assistant_read_provisional_in_use: provisional, assistant_read_excluded: excluded, ignored_invalid_overlay_entries: invalidOverlay, held_by_open_conflict: heldByConflict, ignored_ineligible_overlay_entries: ineligibleOverlay, skipped_intake_entries: skippedIntake.length, skipped_intake_detail: skippedIntake, intake_register_status: reg.status, intake_register_problem: reg.problem ?? null, total: records.length } };
}
