# Audit of the existing Node experiment (done before any change)

Project `D:\Bills`, branch `master`. `experiments/` is **entirely untracked** by git (no history protects it), so before touching anything a SHA-256
manifest (109 files) and a full copy (minus `node_modules`) were written to `experiments/_preservation/`. Nothing in `experiments/node-extraction/`
outside the new `fuel/` folder was modified (checked at the end - see the bottom of this file).

## What "Node" means here

It is a **Node.js program**, an experimental parallel implementation of the application's Python extraction stack, written in order to see how far
JavaScript/npm tooling can go. It is **not** a trainable model and **not** an OCR model.

| Component | What it is |
|---|---|
| `src/pdfExtract.mjs`, `src/textExtraction.mjs` | `pdfjs-dist` 4.10.38 text extraction + a coordinate/line reconstruction heuristic (three signals: pdfjs's own end-of-line flag, a Y jump > 5 pt, an anomalously wide whitespace item) |
| `src/ocrEngine.mjs` | `tesseract.js` 7.0.0 (the WASM build of the Tesseract engine), single pass, English, plain text only (no word boxes, no rotation handling) |
| `src/billParser.mjs`, `dates.mjs`, `money.mjs`, `categoryMap.mjs` | **deterministic regex / string rules** - a line-for-line port of the Python `bill_parser.py` (amount labels with priorities, vendor heuristics, day-first numeric dates) |
| `src/expenseReportParser.mjs` | deterministic parser for the Expense Report's own table |
| `parity-test*.mjs`, `scripts/evaluate-*.mjs` | parity checks against the Python modules on the 6 reference reports (154/154 rows) |

* **Rules / regular expressions:** yes (the whole parser).
* **OCR text extraction:** yes (tesseract.js plain text).
* **Text with bounding boxes / positions:** only for PDF-native text (pdfjs item coordinates). **The OCR path used no positions.**
* **A pretrained OCR model:** yes - Tesseract's English LSTM model (`eng.traineddata`, 5 MB, present in the folder).
* **A pretrained receipt-understanding model:** no.
* **A trained or fine-tuned model:** **no.** The README of the experiment says so explicitly ("No ML training or fine-tuning occurred anywhere in this
  experiment") and I found no training script, no model artefact other than `eng.traineddata`, no labelled training set, and no hyper-parameter or seed
  file anywhere in the tree. `eng.traineddata` is Tesseract's stock model, not something this project trained.

## What the existing pipeline does for a fuel receipt

OCR text -> `parseBillText`: `findVendor` (first vendor-looking line / GSTIN neighbourhood), `findDate` (labelled date, else the only date; numeric
dates are read **day-first by default**), `findAmount` (labelled amount, priority `grand total` > ... > `amount` > `total`; a multi-transaction-statement guard).
It has **no pump-name concept**, no print-date vs transaction-date distinction, no ambiguity state for dates, no per-field review flag (only a bill-level
`EXTRACTED` / `REVIEW_REQUIRED`), and no raw/normalised pair.

## Existing results found

`reports/receipt-extraction-baseline-20261013.md`: on 13 human-verified samples (mostly not fuel) amount accuracy was Python 3/13, Node 5/13;
vendor extraction picked adjacent labels on 5 of 13; these are consistent with what the new baseline below shows for fuel receipts.
`datasets/human-verified-bill-annotations.json` (13 samples, 2 fuel receipts) uses its own schema (vendor/date/totalAmount/invoice number ...) and was
**left untouched**; the new corpus uses a separate annotation file (`fuel/corpus/annotations.json`) with the brief's schema extended by
`acceptable_forms`, `split`, `difficulty` and expected-review flags.

## Preserved, not overwritten

* the current best implementation (`src/*`) is imported **read-only** by `fuel/src/baselineAdapter.mjs`; the improved extractor is new code in `fuel/src/`.
* `eng.traineddata`, `datasets/`, `reports/`, `benchmarks/`, `fixtures/` and `node_modules/` were not modified.
* No model was trained, so nothing was fine-tuned and no model artefact was created.
