# Fuel-receipt extraction experiment (`pump_name`, `transaction_date`, `total_amount`)

**Experimental. Not connected to the production backend or the Electron app. Nothing here was trained or fine-tuned.** It is deterministic rules
over local OCR text plus line geometry. No hosted AI (NVIDIA / DeepSeek stay disabled) and no receipt image or OCR text leaves the machine.

Network disclosure: on the very first OCR run from this folder, tesseract.js downloaded Tesseract's public English language data (`eng.traineddata`, 5.2 MB,
now stored as `fuel/eng.traineddata`, the same size as the copy already in `../`) from its default CDN (cdn.jsdelivr.net). No receipt data was sent. `src/ocr.mjs`
now reads that local file (`langPath`/`cachePath`), and `scripts/verify_offline.mjs` shows OCR + extraction running with `fetch`, `http` and `https` blocked
(0 attempts caught; results identical to the cached runs).

**Phase 2 (read first): `REPORT_PHASE2.md`** - label-verification workflow (`verification/`), intake and held-out reservation (`intake/`), the matching-safety contract (`src/matchSafety.mjs`), the bounded OCR retry, and corrected results. `REPORT.md` is phase 1 (see its correction notice). Read `REPORT.md` for the phase-1 results and their limits, `AUDIT.md` for what the existing experiment is, `RESEARCH.md` + `sources.json` for the
source inventory, and `INTEGRATION_NOTES.md` for how a later, separately authorised integration should use this.

## Scope (strict)

Required output: `pump_name`, `transaction_date` (`YYYY-MM-DD` only when supported by evidence), `total_amount`. Rate, litres, fuel type, nozzle/FIP,
vehicle details etc. are **not** extracted, scored or used for matching; the extractor only recognises their *labels* so their numbers are not mistaken for the total.

## Layout

```
fuel/
  src/ocr.mjs              local tesseract.js OCR with line/word geometry, disk cache, and an optional bounded retry (preprocess + rotations)
  src/png.mjs              dependency-free PNG decode/encode for the retry preprocessing (node:zlib only)
  src/fuelExtractor.mjs    the improved extractor and its interface
  src/baselineAdapter.mjs  the EXISTING pipeline (../../src/billParser.mjs, unmodified) mapped onto the same interface
  src/scoring.mjs          metrics + the normalisation policy
  corpus/                  authentic images, synthetic images, annotations.json, split_freeze.json, ocr-cache/
  scripts/                 corpus builders, eval runner, report generator, dev-only diagnostics
  tests/                   unit tests of the extraction rules (node --test tests/fuelExtractor.test.mjs)
  reports/                 every evaluation run (json + details) and the generated RESULTS.md
```

Reproduce (from `experiments/node-extraction/fuel`):
```
node --test tests/fuelExtractor.test.mjs
node scripts/ocr_corpus.mjs                       # one-off OCR (cached under corpus/ocr-cache)
node scripts/run_eval.mjs --impl baseline --label baseline
node scripts/run_eval.mjs --impl improved --label improved      # improved = OCR with bounded retry + new extractor
node scripts/make_report.mjs                      # writes reports/RESULTS.md from the stored runs
```
(`python scripts/make_synthetic_fuel.py`, `make_synthetic_fuel_heldout2.py`, `build_annotations.py` regenerate the synthetic images and annotations;
`census_fuel_pages.py` / `render_authentic_pages.py` found and rendered the authentic pages from the approved reports, read-only.)

## Interface

`extractFuelReceipt(ocr) -> { pump_name, transaction_date, total_amount, validation_warnings, overall_review_required, _meta }` where `ocr` is the
output of `CachedOCR.read()` (`text`, `confidence`, `lines[{text,bbox,confidence,words[]}]`). Each field is

```
{ raw_value, normalized_value, confidence, confidence_type: "heuristic_uncalibrated",
  evidence: [{ role, line_index, text, ocr_line_confidence, bbox, brand? }], ambiguity, review_required,
  candidates?, basis }
```

* `raw_value` is the printed text; `normalized_value` is `null` unless supported by evidence. Missing, ambiguous and confidently-read are distinct:
  `normalized_value === null` = not established; `review_required === true` with a value = best candidate, a person must confirm; `review_required === false` = fit for automatic use.
* `confidence` is a **heuristic** (OCR confidence scaled down when review is required). It is **not** a calibrated probability and no threshold here is calibrated; the gates
  (`PUMP_MIN_WORD_CONF` 0.8, `TOKEN_MIN_CONF` 0.7, line confidence 0.55 / 0.6) were chosen on development receipts only.
* `ambiguity` explains why review is required (e.g. both candidate dates for an unprovable day/month order).
* `overall_review_required` is true if any field is null or flagged, or the document has no fuel-receipt evidence.

## Rules in force

**pump_name** - the printed dealer/station name is preferred (a header line with a dealer-type cue: FUEL(S), PETROLEUM, SERVICE, STATION, AGENCIES, FILLING, CENTRE, a `BP/HP <name>` form, a line followed by `BPCL DEALER`, or a printed `Dealer:` label). Brand (BPCL / HPCL / IOCL / Nayara / Jio-bp / Shell, tolerating one OCR character error) is kept as evidence; a brand-only result is flagged for review. A header line with no dealer cue is never presented as the station when the brand is known (it may be a place name). Greetings, logo-wordmark fragments, OCR noise, address/GST/phone lines are rejected. A name made from words the OCR was unsure of, or trimmed because edge words were weak, requires review.

**transaction_date** - the receipt's own transaction date, not the print date (`Printed on`, `Reprinted`, ... are recorded as evidence and not used). Numeric day/month order is resolved **only** by: an unambiguous reading (a component > 12, or both equal); an explicit `DD/MM/YYYY`-style cue; another unambiguous date on the same receipt in the same printed format; or a month printed in the bill number ("Bill No: Sep-894177") that agrees with exactly one reading. Otherwise the value stays `null`, **both readings are kept in `candidates`**, and review is required. The claimed date is not an input. Dates read after letter/digit OCR repair are never auto-usable.

**total_amount** - the labelled total (`Total Amount`, `Sale Amount`, `Net Amount`, `Amount(Rs)`, `Amount`, `Sale`, `Total` ...), on the same or the next line; numbers under rate / volume / density / totaliser / preset / balance / points / ID labels are never candidates. Two different amounts at the same priority, a different amount under a lower-priority label, or another currency figure not explained by such a label -> review. Never computed from rate x quantity. Approximate labels (`fnount(Rs.)`) and repaired cents (`2000. G0`) are always flagged.

## Known limitations (also in REPORT.md)

* The authentic corpus is 22 receipts of three brands from 7 reports; labels are assistant-verified, not independently human-verified. No authentic Nayara / Jio-bp / Shell receipt was available, so those brands are exercised only by synthetic receipts.
* OCR is the dominant error source (blur, dark backgrounds, overlay text, sideways margin text, large print). The retry helps but does not remove it.
* Column-table layouts (header row of labels, values on the next line), unfamiliar total labels ('Net Payable'), dotted 2-digit-year dates, and handwriting are not reliably read; the extractor then returns null + review rather than a guess. Handwriting accuracy is **not** claimed: the one handwritten receipt has a label read by the assistant only.
* Date ambiguity is common on real receipts (day and month both <= 12); without evidence they correctly stay unresolved, which is why most authentic receipts still require review.
