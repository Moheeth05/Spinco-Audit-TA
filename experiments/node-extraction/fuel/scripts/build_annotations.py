"""Single source of truth for the fuel-receipt annotations. Writes:
  corpus/annotations.json         every receipt (authentic + synthetic + controls) in one schema
  corpus/split_freeze.json        the dev / held-out family split with a timestamp and content hash

LABEL PROVENANCE (read this before trusting any number):
* Authentic labels were read by the assistant directly from the rendered page images (not from any extractor output).
  They are PROVISIONAL ('provisional_assistant_read'): NOT verified by any human. They must not be called verified until an authorized human has checked them with the workflow in fuel/verification/. If company policy forbids AI review of the receipts they must be excluded (verification/label_policy.json).
* Synthetic / control labels are exact BY CONSTRUCTION (generator), and are 'construction_truth'.
* Date normalisation in a label is the receipt's true date. `date_order_evidence` records what in the receipt (or, for
  ambiguous ones, which sibling receipts) establishes the day/month order, so the scorer can tell 'the receipt alone
  proves it' from 'a careful reader still has to abstain'.

The authentic split below was fixed BEFORE any OCR output for the authentic pages was read or any extractor was written.
Note the honest limit: labelling required looking at every page, so the *layouts* of the held-out families are known to the
author; what is held out is the OCR behaviour / failure modes, which were never inspected for those families.
"""
import hashlib, json
from datetime import datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
CORPUS = HERE / "corpus"

# (id, report, page, family, split, brand, difficulty tags, pump{raw, forms, logo_only}, date{raw, iso, order_evidence, ambiguous_alone}, amount{raw, value}, notes)
A = []


def add(rid, family, split, brand, diff, pump_raw, forms, date_raw, iso, order_ev, amb, amt_raw, amt, logo_only=False, notes=""):
    A.append(dict(rid=rid, family=family, split=split, brand=brand, diff=diff, pump_raw=pump_raw, forms=forms, logo_only=logo_only,
                  date_raw=date_raw, iso=iso, order_ev=order_ev, amb=amb, amt_raw=amt_raw, amt=amt, notes=notes))


# ---- DEV: family A "bpcl_pos_txn" (BayNo / Txn Id / TxSt / TxEnd), BPCL-branded, ER-39832
add("auth_ER-39832_p03", "bpcl_pos_txn", "dev", "BPCL", ["photographed", "dark_background", "blurry", "faint_print"], "THENETH AUTO FUELS", ["THENETH AUTO FUELS", "THENETH AUTOFUELS"],
    "01-06-2026", "2026-06-01", "none_on_receipt (01/06 both <=12); sibling receipts of this template print 22/23-06-2026", True, "2000.00", 2000.00,
    notes="Date separators faint in the photo; TxSt/TxEnd repeat 01-06-26.")
add("auth_ER-39832_p05", "bpcl_pos_txn", "dev", "BPCL", ["photographed", "dark_background"], "BP IRIMPANAM", ["BP IRIMPANAM", "BHARAT PETROLEUM CORP BP IRIMPANAM"],
    "08-06-2026", "2026-06-08", "none_on_receipt (08/06 both <=12); siblings print 22/23-06-2026", True, "2000.00", 2000.00,
    notes="Preset Value also 2000.00 (same number, not a different amount). The earlier OCR read this date as 18-06-2026: an OCR error.")
add("auth_ER-39832_p11", "bpcl_pos_txn", "dev", "BPCL", ["photographed", "dark_background", "sideways_margin_text"], "CCM FUELS", ["CCM FUELS"],
    "22-06-2026", "2026-06-22", "unambiguous_day_gt_12", False, "1000.00", 1000.00, notes="Header reads WELCOME TO BPCL / CCM FUELS / KOLANCHERY- ERNAKULAM.")
add("auth_ER-39832_p12", "bpcl_pos_txn", "dev", "BPCL", ["photographed", "dark_background"], "BP IRIMPANAM", ["BP IRIMPANAM", "BHARAT PETROLEUM CORP BP IRIMPANAM"],
    "23-06-2026", "2026-06-23", "unambiguous_day_gt_12", False, "2000.00", 2000.00)

# ---- DEV: family B "orgnl_physical_receipt" (Bill/INV No ...-ORGNL, Physical Receipt)
add("auth_ER-39832_p04", "orgnl_physical_receipt", "dev", "BPCL", ["photographed", "dark_background"], "THENETH AUTOFUELS", ["THENETH AUTOFUELS", "THENETH AUTO FUELS"],
    "04/06/2026", "2026-06-04", "none_on_receipt (04/06 both <=12); 'Jun' in the INV NO prefix agrees with June", True, "Rs.1500.00", 1500.00)
add("auth_ER-39832_p08", "orgnl_physical_receipt", "dev", "BPCL", ["photographed", "dark_background"], "THENETH AUTOFUELS", ["THENETH AUTOFUELS", "THENETH AUTO FUELS"],
    "16/06/2026", "2026-06-16", "unambiguous_day_gt_12", False, "Rs.1500.00", 1500.00)
add("auth_ER-39832_p13", "orgnl_physical_receipt", "dev", "BPCL", ["photographed", "dark_background"], "THENETH AUTOFUELS", ["THENETH AUTOFUELS", "THENETH AUTO FUELS"],
    "26/06/2026", "2026-06-26", "unambiguous_day_gt_12", False, "Rs.1500.00", 1500.00)
add("auth_ER-40223_p21", "orgnl_physical_receipt", "dev", "BPCL", ["scanned_page", "tiny_receipt_on_large_page"], "SHRESHTHA", ["SHRESHTHA", "SHRESHTHA BPCL DEALER"],
    "31/07/2026", "2026-07-31", "unambiguous_day_gt_12", False, "Rs.4148.19", 4148.19, notes="Small receipt in the middle of an A4 scan.")
add("auth_ER-40706_p17", "orgnl_physical_receipt", "dev", "HP", ["scanned_page", "tall_crop", "logo_only_brand"], None, [],
    "20/07/2026", "2026-07-20", "unambiguous_day_gt_12", False, "Rs.3947.02", 3947.02, logo_only=True,
    notes="No dealer or brand TEXT is printed; the HP logo (wordmark 'HP') is the only brand evidence. The receipt top is cropped.")
add("auth_ER-40706_p18", "orgnl_physical_receipt", "dev", "HP", ["scanned_page", "tall_crop", "overlay_text_ghosting"], "S V F F S", ["SVFFS", "HP DEALER OUTLET", "SVFFS HP DEALER OUTLET"],
    "24/07/2026", "2026-07-24", "unambiguous_day_gt_12", False, "Rs.3612.66", 3612.66, notes="Dealer name is spaced initials; ghost 'HDFC BANK' paper text in the margin.")
add("auth_ER-40513_p06", "orgnl_physical_receipt", "dev", "IndianOil", ["photographed", "overlay_text_ghosting", "stray_overlay_over_text"], "CHANKYAPURI SSTN", ["CHANKYAPURI SSTN"],
    "02/09/2026", "2026-09-02", "none_on_receipt (02/09 both <=12); 'Sep' in the Bill No prefix agrees with September", True, "Rs.3687.55", 3687.55,
    notes="'SBI Payments' overlay text partially covers the lower-left of the receipt.")
add("auth_ER-40513_p22", "orgnl_physical_receipt", "dev", "HP", ["photographed", "textured_background"], "GAURI SHIVAAY PETROLEUM", ["GAURI SHIVAAY PETROLEUM"],
    "18/09/2026", "2026-09-18", "unambiguous_day_gt_12", False, "Rs.2694.47", 2694.47)

# ---- HELD-OUT: family C "welcomes_you_pinelabs" (Receipt/Local ID, FIP, Nozzle, Density, Rate, Volume, Amount(Rs), Date: dd/mm/yy)
add("auth_ER-39832_p07", "welcomes_you_pinelabs", "heldout", "BPCL", ["photographed", "dark_background", "totaliser_decoys"], "PARAMOUNT AGENCIES", ["PARAMOUNT AGENCIES", "PARAMOUNT AGENCIES BPCL DEALER"],
    "12/06/26", "2026-06-12", "none_on_receipt (12/06 both <=12); 'Printed on' repeats the same date", True, "01000.00", 1000.00,
    notes="Atot/Vtot totalisers are large decoy numbers; two dates (Date and Printed on) agree.")
add("auth_ER-39832_p14", "welcomes_you_pinelabs", "heldout", "BPCL", ["photographed", "dark_background", "logo_only_brand", "sideways_margin_text"], "Bharat Petroleum", ["BHARAT PETROLEUM"],
    "29/06/26", "2026-06-29", "unambiguous_day_gt_12", False, "02200.00", 2200.00, logo_only=True,
    notes="No dealer name is printed (Tel. No. blank); the only brand evidence is the logo's own wordmark 'Bharat Petroleum'.")
add("auth_ER-40706_p16", "welcomes_you_pinelabs", "heldout", "HP", ["scanned_page", "tall_crop"], "SIRI AADHYA RUDRA FUEL", ["SIRI AADHYA RUDRA FUEL", "SIRI AADHYA RUDRA FUELS"],
    "14/07/26", "2026-07-14", "unambiguous_day_gt_12", False, "03908.04", 3908.04, notes="'Printed on' repeats the same date.")
add("auth_ER-40782_p04", "welcomes_you_pinelabs", "heldout", "IndianOil", ["scanned_page", "tall_crop", "duplicate_copy", "sideways_margin_text"], "UNNATI PETROLEUM", ["UNNATI PETROLEUM", "UNNATI PETROLEUN"],
    "07/07/26", "2026-07-07", "order_independent (day == month == 7)", False, "03900.15", 3900.15,
    notes="'Duplicate Receipt Copy'. The last letter of PETROLEUM prints as N/M ambiguously. The page is cut off just below 'Time: 20:50'.")
add("auth_ER-40782_p08", "welcomes_you_pinelabs", "heldout", "IndianOil", ["photographed", "tall_crop", "creased_paper"], "UNNATI PETROLEUM", ["UNNATI PETROLEUM"],
    "13/07/26", "2026-07-13", "unambiguous_day_gt_12", False, "03916.80", 3916.80)
add("auth_ER-40513_p24", "welcomes_you_pinelabs", "heldout", "IndianOil", ["photographed", "textured_background"], "KHYBER SERVICE STATION", ["KHYBER SERVICE STATION"],
    "24/09/26", "2026-09-24", "unambiguous_day_gt_12", False, "03528.45", 3528.45)
add("auth_ER-41115_p06", "welcomes_you_pinelabs", "heldout", "BPCL", ["scanned_page", "tall_crop", "sideways_margin_text"], "ONKAR SERVICE", ["ONKAR SERVICE"],
    "09/08/26", "2026-08-09", "none_on_receipt (09/08 both <=12); a sibling receipt of this template prints 25/08/26", True, "04499.77", 4499.77,
    notes="'Printed on' repeats 09/08/26.")
add("auth_ER-41115_p49", "welcomes_you_pinelabs", "heldout", "BPCL", ["scanned_page", "tall_crop", "sideways_margin_text"], "ONKAR SERVICE", ["ONKAR SERVICE"],
    "25/08/26", "2026-08-25", "unambiguous_day_gt_12", False, "04762.93", 4762.93)

# ---- HELD-OUT singleton families
add("auth_ER-40706_p19", "handwritten_dealer_invoice", "heldout", "HP", ["scanned_page", "rotated_90", "handwritten", "printed_letterhead"], "LALITHA GOWRI PETROLEUM CENTRE", ["LALITHA GOWRI PETROLEUM CENTRE"],
    "29/07/26", "2026-07-29", "unambiguous_day_gt_12", False, "3423", 3423.00,
    notes="HANDWRITTEN date and amounts on a printed HP dealer letterhead (page rotated 90 degrees). Handwriting read by the assistant only; NOT a verified handwriting label. Rate 116.50 is also handwritten.")
add("auth_ER-41220_p06", "hp_original_star", "heldout", "HP", ["photographed", "large_print"], "AUTO CARE CENTRE", ["AUTO CARE CENTRE", "HP AUTO CARE CENTRE"],
    "16-SEP-2026", "2026-09-16", "month_name", False, "2500.00 INR", 2500.00, notes="TXN NO and INVOICE NO are long numbers next to the date block (decoys).")

# ---- not authentic: suspected template-generated
SUSPECT = dict(rid="auth_ER-41115_p50", pump_raw="Bharat Petroleum", forms=["BHARAT PETROLEUM"], date_raw="2026-09-23", iso="2026-09-23", amt_raw="1000", amt=1000.00)

rows = []
for a in A:
    rows.append({
        "receipt_id": a["rid"], "source_type": "authentic", "source_reference": f"approved test report {a['rid'].split('_')[1]} page {int(a['rid'].split('_p')[1])} (local, not redistributable)",
        "receipt_family": a["family"], "split": a["split"], "brand_family": a["brand"], "difficulty": a["diff"],
        "fields": {
            "pump_name": {"raw_value": a["pump_raw"], "normalized_value": (a["forms"][0] if a["forms"] else None), "acceptable_forms": a["forms"], "confidence": None,
                          "evidence": ["printed header text" if not a["logo_only"] else "brand wordmark inside the logo graphic only (no printed dealer/brand line)"], "review_required": bool(a["logo_only"]),
                          "evidence_source": "logo_wordmark" if a["logo_only"] else "printed_text"},
            "transaction_date": {"raw_value": a["date_raw"], "normalized_value": a["iso"], "confidence": None, "evidence": [f"order evidence: {a['order_ev']}"], "review_required": bool(a["amb"]),
                                 "ambiguous_on_receipt_alone": bool(a["amb"]), "order_evidence": a["order_ev"]},
            "total_amount": {"raw_value": a["amt_raw"], "normalized_value": a["amt"], "confidence": None, "evidence": ["printed amount line"], "review_required": False},
        },
        "annotation_status": "provisional_assistant_read", "label_policy_status": "pending_authorized_human_verification", "manually_reviewed_by_independent_human": False, "notes": [a["notes"]] if a["notes"] else [],
    })

# synthetic + controls -> same schema
syn = json.loads((CORPUS / "synthetic" / "synthetic_annotations.json").read_text(encoding="utf-8"))
for r in syn["records"]:
    t = r["truth"]
    forms = [t["pump_name"]] + list(t.get("pump_alternates") or []) if t.get("pump_name") else []
    # LABEL-POLICY CORRECTION (made after the first held-out look, disclosed in the report): the authentic labelling policy accepts the
    # printed DEALER name on its own; the jio-bp / Shell synthetic labels had only "<brand> <dealer>" and so rejected a correct dealer-only
    # answer. Add the dealer-only form for those two templates. Generator, images and printed text are unchanged.
    for prefix in ("jio-bp ", "Shell "):
        if t.get("pump_name") and t["pump_name"].startswith(prefix):
            forms.append(t["pump_name"][len(prefix):])
    is_ctl = r["kind"] == "non_fuel"
    ambiguous = bool(t.get("expect_review")) and t.get("review_reason", "").startswith("ambiguous")
    rows.append({
        "receipt_id": r["receipt_id"], "source_type": r["source_type"], "source_reference": r["source_reference"], "receipt_family": r["receipt_family"], "split": r["split"],
        "brand_family": None, "difficulty": [r["difficulty"]],
        "fields": {
            "pump_name": {"raw_value": t.get("pump_name"), "normalized_value": t.get("pump_name"), "acceptable_forms": [f for f in forms if f] if not is_ctl else [], "confidence": None,
                          "evidence": [], "review_required": is_ctl or t.get("pump_name") is None},
            "transaction_date": {"raw_value": t.get("date_raw"), "normalized_value": t.get("date"), "confidence": None, "evidence": [], "review_required": ambiguous or is_ctl,
                                 "ambiguous_on_receipt_alone": ambiguous},
            "total_amount": {"raw_value": t.get("amount_raw"), "normalized_value": t.get("amount"), "confidence": None, "evidence": [], "review_required": False},
        },
        "expect_review": bool(t.get("expect_review")), "review_reason": t.get("review_reason"), "kind": r["kind"],
        "annotation_status": r["annotation_status"], "manually_reviewed_by_independent_human": False, "notes": [],
    })

# second synthetic held-out set (written after the first held-out look; see make_synthetic_fuel_heldout2.py)
syn2_path = CORPUS / "synthetic2" / "synthetic2_annotations.json"
if syn2_path.exists():
    for r in json.loads(syn2_path.read_text(encoding="utf-8"))["records"]:
        t = r["truth"]
        forms = ([t["pump_name"]] + list(t.get("pump_alternates") or [])) if t.get("pump_name") else []
        for prefix in ("jio-bp ", "Shell "):
            if t.get("pump_name") and t["pump_name"].startswith(prefix):
                forms.append(t["pump_name"][len(prefix):])
        rows.append({
            "receipt_id": r["receipt_id"], "source_type": "synthetic", "source_reference": r["source_reference"], "receipt_family": r["receipt_family"], "split": "heldout2",
            "brand_family": None, "difficulty": [r["difficulty"]],
            "fields": {
                "pump_name": {"raw_value": t.get("pump_name"), "normalized_value": t.get("pump_name"), "acceptable_forms": forms, "confidence": None, "evidence": [], "review_required": t.get("pump_name") is None},
                "transaction_date": {"raw_value": t.get("date_raw"), "normalized_value": t.get("date"), "confidence": None, "evidence": [], "review_required": False, "ambiguous_on_receipt_alone": bool(t.get("expect_review")) and str(t.get("review_reason", "")).startswith("ambiguous")},
                "total_amount": {"raw_value": t.get("amount_raw"), "normalized_value": t.get("amount"), "confidence": None, "evidence": [], "review_required": False},
            },
            "expect_review": bool(t.get("expect_review")), "review_reason": t.get("review_reason"), "kind": "fuel",
            "annotation_status": "construction_truth", "manually_reviewed_by_independent_human": False, "notes": [],
        })

# third synthetic held-out set (priority brands; see make_synthetic_fuel_heldout3.py)
syn3_path = CORPUS / "synthetic3" / "synthetic3_annotations.json"
if syn3_path.exists():
    for r in json.loads(syn3_path.read_text(encoding="utf-8"))["records"]:
        t = r["truth"]
        forms = ([t["pump_name"]] + list(t.get("pump_alternates") or [])) if t.get("pump_name") else []
        for prefix in ("jio-bp ", "Shell "):
            if t.get("pump_name") and t["pump_name"].startswith(prefix):
                forms.append(t["pump_name"][len(prefix):])
        rows.append({
            "receipt_id": r["receipt_id"], "source_type": "synthetic", "source_reference": r["source_reference"], "receipt_family": r["receipt_family"], "split": "heldout3",
            "brand_family": None, "difficulty": [r["difficulty"]],
            "fields": {
                "pump_name": {"raw_value": t.get("pump_name"), "normalized_value": t.get("pump_name"), "acceptable_forms": forms, "confidence": None, "evidence": [], "review_required": t.get("pump_name") is None},
                "transaction_date": {"raw_value": t.get("date_raw"), "normalized_value": t.get("date"), "confidence": None, "evidence": [], "review_required": False, "ambiguous_on_receipt_alone": False},
                "total_amount": {"raw_value": t.get("amount_raw"), "normalized_value": t.get("amount"), "confidence": None, "evidence": [], "review_required": False},
            },
            "expect_review": bool(t.get("expect_review")), "review_reason": t.get("review_reason"), "kind": "fuel",
            "annotation_status": "construction_truth", "manually_reviewed_by_independent_human": False, "notes": [],
        })

# fourth synthetic held-out set (priority brands, fresh layouts; see make_synthetic_fuel_heldout4.py)
syn4_path = CORPUS / "synthetic4" / "synthetic4_annotations.json"
if syn4_path.exists():
    for r in json.loads(syn4_path.read_text(encoding="utf-8"))["records"]:
        t = r["truth"]
        forms = ([t["pump_name"]] + list(t.get("pump_alternates") or [])) if t.get("pump_name") else []
        for prefix in ("jio-bp ", "Shell "):
            if t.get("pump_name") and t["pump_name"].startswith(prefix):
                forms.append(t["pump_name"][len(prefix):])
        rows.append({
            "receipt_id": r["receipt_id"], "source_type": "synthetic", "source_reference": r["source_reference"], "receipt_family": r["receipt_family"], "split": "heldout4",
            "brand_family": None, "difficulty": [r["difficulty"]],
            "fields": {
                "pump_name": {"raw_value": t.get("pump_name"), "normalized_value": t.get("pump_name"), "acceptable_forms": forms, "confidence": None, "evidence": [], "review_required": t.get("pump_name") is None},
                "transaction_date": {"raw_value": t.get("date_raw"), "normalized_value": t.get("date"), "confidence": None, "evidence": [], "review_required": False, "ambiguous_on_receipt_alone": bool(t.get("expect_review")) and str(t.get("review_reason", "")).startswith("ambiguous")},
                "total_amount": {"raw_value": t.get("amount_raw"), "normalized_value": t.get("amount"), "confidence": None, "evidence": [], "review_required": False},
            },
            "expect_review": bool(t.get("expect_review")), "review_reason": t.get("review_reason"), "kind": "fuel",
            "annotation_status": "construction_truth", "manually_reviewed_by_independent_human": False, "notes": [],
        })
# the suspected template-generated page, kept OUT of the authentic group
rows.append({
    "receipt_id": SUSPECT["rid"], "source_type": "suspected_template_generated", "source_reference": "approved test report ER-41115 page 50 (local)",
    "receipt_family": "generator_crumpled_template", "split": "heldout", "brand_family": "BPCL", "difficulty": ["low_contrast", "crumpled_texture", "image_rendering"],
    "fields": {
        "pump_name": {"raw_value": SUSPECT["pump_raw"], "normalized_value": SUSPECT["pump_raw"], "acceptable_forms": SUSPECT["forms"], "confidence": None, "evidence": ["printed 'Bharat Petroleum' under WELCOME!!!"], "review_required": False},
        "transaction_date": {"raw_value": SUSPECT["date_raw"], "normalized_value": SUSPECT["iso"], "confidence": None, "evidence": ["ISO date: unambiguous"], "review_required": False, "ambiguous_on_receipt_alone": False},
        "total_amount": {"raw_value": SUSPECT["amt_raw"], "normalized_value": SUSPECT["amt"], "confidence": None, "evidence": ["AMOUNT: 1000 (no decimals)"], "review_required": False},
    },
    "annotation_status": "provisional_assistant_read", "label_policy_status": "pending_authorized_human_verification", "manually_reviewed_by_independent_human": False,
    "notes": ["Layout matches the public generator sites' templates (WELCOME banner, blank FCC/FIP/NOZZLE rows, 'crumpled' texture, 'Save Fuel, Save Money'); NOT counted as an authentic receipt. This is an observation about layout, not an allegation: a human should look at it."],
})

# LABEL-POLICY CORRECTION 2 (phase 2, applied UNIFORMLY to every synthetic set): the generators listed the oil-company BRAND ("NAYARA ENERGY", "IOCL", "HP", ...) among the
# acceptable pump names, so a brand-only answer was scored as exact even though a dealer name is printed. The scoring policy (src/scoring.mjs, and the authentic labels) says a
# brand-only answer is NOT exact when a dealer name is printed. Brand-only forms are removed from every synthetic receipt whose primary label is a dealer name.
import re as _re
_BRAND_ONLY = _re.compile(r"^(INDIANOIL|IOCL|IOC|BHARATPETROLEUM(CORP)?|BPCL|HP|HPCL|HINDUSTANPETROLEUM|NAYARA(ENERGY)?|JIOBP|SHELL(RETAILINDIA|SELECTSTATION)?|XTRAREWARDS)$")
def _n(x):
    return _re.sub(r"[^A-Z0-9]", "", str(x).upper())
for _r in rows:
    if _r["source_type"] == "synthetic" and _r.get("kind") == "fuel":
        _f = _r["fields"]["pump_name"]
        if _f.get("normalized_value") and not _BRAND_ONLY.match(_n(_f["normalized_value"])):
            _f["acceptable_forms"] = [x for x in _f.get("acceptable_forms", []) if not _BRAND_ONLY.match(_n(x))]
(CORPUS / "annotations.json").write_text(json.dumps({"schema": "fuel-receipt-annotation/v1 (adapted from the brief's illustrative schema; adds acceptable_forms, split, difficulty, expected-review flags)",
                                                     "generated": datetime.now().isoformat(timespec="seconds"), "records": rows}, indent=1, ensure_ascii=False), encoding="utf-8")
split = {"frozen_at": datetime.now().isoformat(timespec="seconds"),
         "rule": "Whole layout families go to one side. Authentic: dev = bpcl_pos_txn + orgnl_physical_receipt; heldout = welcomes_you_pinelabs + handwritten_dealer_invoice + hp_original_star. Synthetic: dev vs heldout TEMPLATES (see synthetic generator).",
         "frozen_before": "any OCR output for the authentic pages was read, and before any extractor code for this task was written",
         "families": {}}
for r in rows:
    split["families"].setdefault(r["split"], {}).setdefault(f"{r['source_type']}:{r['receipt_family']}", []).append(r["receipt_id"])
split["sha256_of_annotations"] = hashlib.sha256((CORPUS / "annotations.json").read_bytes()).hexdigest()
(CORPUS / "split_freeze.json").write_text(json.dumps(split, indent=1), encoding="utf-8")
from collections import Counter
print(Counter((r["source_type"], r["split"]) for r in rows))
