# Handwritten Bill OCR — Ground-Truth Evaluation

Isolated evaluation. No production file was modified by this benchmark itself (the production changes it evaluates are in `backend/app/services/extraction/ocr_engine.py` and `text_extraction.py`, committed separately per the main task report).

## Ground truth (recorded by directly viewing each rendered page before running any OCR — never from a system prediction)

**RATHNA ELECTRICALS & HARDWARES** (`ER-39832.pdf` page 6): vendor "RATHNA ELECTRICALS & HARDWARES", invoice no. 664, GSTIN `32ARWPK8203C1ZW`, amount 220 (handwritten "GRAND TOTAL ... 220/-"), date handwritten, digits read as 9/6/2026 (day/month order ambiguous, as with any numeric handwritten date — not resolved further here).

**DISHANT PETROLEUM SERVICE** (`vivek-bhai-report-ER41220.pdf` page 7): vendor "DISHANT PETROLEUM SERVICE", invoice no. 648591, GSTIN `24AKHPP8768M1ZA`, amount 300 (handwritten, appears twice), date handwritten, digits read as 17/9/26.

## A real, unplanned finding: the RATHNA page is genuinely rotated

While rendering pages to independently verify ground truth, the RATHNA page turned out to be physically sideways as rendered by PyMuPDF (confirmed: `page.rotation == 0` — no PDF-level rotation metadata; the embedded photograph itself is landscape). This was not constructed for the test — it's a real example already present in the existing fixture corpus. `ocr_engine._correct_orientation` correctly detected it: `OSD rotate=270, orientation_conf=10.26` (well above the 1.0 threshold) and corrected it to upright. The Dishant Petroleum page was already upright; `rotation_applied=0`, correctly making no change.

## Before/after results

| | RATHNA (rotated) | Dishant Petroleum |
|---|---|---|
| Rotation applied | **270°** (real, confirmed) | 0° (already upright) |
| Text length | 668 → **702** chars | 59 → 59 chars (unchanged) |
| OCR confidence | 0.487 → **0.550** | 0.287 → 0.287 (unchanged) |
| Vendor extracted | ✗ both ("ELECTRICALS & HARDWARES b Load") | ✗ both (garbled, unrelated OCR noise) |
| Invoice number | ✗ both (None) | ✗ both (None) |
| GSTIN | ✓ both (already correct, unaffected) | ✗ both (None) |
| Amount | ✗ both (None) | ✗ both (None) |
| Date | ✗ both (not parsed; ground-truth digits not found in raw OCR text either before or after) | ✗ both |
| Final status | REVIEW_REQUIRED (LOW) both before/after | REVIEW_REQUIRED (LOW) both before/after |
| Runtime per OCR call | 1806ms → 4224ms | 1220ms → 2505ms |

## Honest interpretation

**Rotation correction works and is confirmed on a real document**, with a measurable (if modest) improvement in raw OCR text quality (+34 characters, +0.063 confidence) once the page is presented upright instead of sideways. This is a genuine fix for a real class of problem (misoriented scans), not a synthetic demonstration.

**It does not, by itself, solve handwriting legibility.** Neither real case recovers the handwritten vendor name, amount, or date — both before and after, for both documents. This is consistent with, and reinforces, the already-existing, previously-confirmed finding in this codebase (`tests/test_bills.py`'s "TAMANG TAXI SERVICE" fixture: "the handwritten amount ... and handwritten date do not [OCR], under Tesseract at any tested PSM mode — a genuine, confirmed limitation"). Rotation and rendering quality are a different problem from recognizing handwritten strokes; fixing the former does not fix the latter. **I am not claiming handwriting recognition is fixed** — only that the orientation and preprocessing infrastructure now exists, is correct, and causes no regression.

**No regression**: both real cases produce the identical final `REVIEW_REQUIRED (LOW)` status before and after — exactly correct, since no field could be confidently extracted either way, and the pipeline correctly declines to fabricate a value in both cases (confirmed by `parse_bill_text`'s own non-fabrication guarantee, unchanged by this task).

**Preprocessing fallback did not get a real trigger opportunity in this corpus.** It only activates when the default pass AND the plain higher-DPI retry both return *completely empty* text — neither real fixture (nor any other known page in the corpus, per the prior session's OCR-retry investigation) hits that specific combination; every known hard case returns at least *some* (if unhelpful) text at 300 DPI already. It is implemented and unit-tested (confirmed it fires and adopts a successful result when both prior stages are empty, via a synthetic test — see `tests/test_extraction.py::test_preprocessed_retry_recovers_text_when_plain_retry_still_empty`), as defensive coverage for a genuinely-blank/very-low-quality scan that hasn't yet been observed in this specific corpus, not as a confirmed real-world fix.

## Runtime cost (measured, real)

OCR runtime per page increased by roughly **1.3-2.3x** across both cases (1806ms→4224ms, 1220ms→2505ms) because orientation detection (`pytesseract.image_to_osd`) now runs unconditionally before every OCR call, in addition to the main recognition pass. This is a secondary Tesseract invocation, not a full extra OCR pass, but it is not free. It applies only to pages that already needed OCR (pages with usable native PDF text never reach this code at all), but for those pages the added cost is real and worth knowing before this ships.

## Remaining limitations

- Only 2 real handwritten fixtures were evaluated with a confirmed, independently-recorded ground truth; a 3rd known hard case ("TAMANG TAXI SERVICE", referenced in the existing test suite) could not be re-evaluated at the image level — the original source PDF for that specific fixture was not available in the current corpus, only its previously-captured OCR text. Its existing regression tests (unaffected by this task's changes, since `bill_parser.py` itself was not touched) continue to pass, confirmed separately.
- No real rotated-page example was available to calibrate the OSD confidence threshold (`_MIN_OSD_CONFIDENCE = 1.0`) *before* this evaluation — the RATHNA page serendipitously turned out to be one, and the chosen threshold worked correctly on it (confidence 10.26, well clear of the 1.0 bar), but the threshold itself remains a documented judgment call, not independently cross-validated against a second real rotated example.
- Handwriting recognition itself remains a genuine, unsolved limitation of local Tesseract for these documents — this task did not claim otherwise, per its own instruction not to overclaim.

## Changed files evaluated by this benchmark

- `backend/app/services/extraction/ocr_engine.py` — `_correct_orientation`, `preprocess_for_difficult_ocr`, wired into `extract_text()`.
- `backend/app/services/extraction/text_extraction.py` — `_retry_ocr_at_higher_dpi` extended with the preprocessing fallback stage.

Both are covered by unit tests in `backend/tests/test_handwritten_ocr.py` and updated/added tests in `backend/tests/test_extraction.py`.
