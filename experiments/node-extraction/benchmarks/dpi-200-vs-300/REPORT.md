# Benchmark: Python OCR Rendering at 200 DPI vs 300 DPI

Isolated evaluation only. No production file was modified. See "Changed files" at the end.

## Baseline (read, not modified)

- `backend/app/services/extraction/text_extraction.py`:
  - `split_pages()` (line 150-160): `has_selectable_text = len(doc[i].get_text().strip()) >= MIN_NATIVE_TEXT_CHARS` (20 chars). This decides whether a page reaches OCR at all, and is computed **purely from the PDF's native text layer** - it never reads a `dpi` value, so render DPI cannot affect which pages get OCR'd.
  - `_apply_ocr()` (line 164-207): for every page that failed the native-text check, renders via `doc[page_number-1].get_pixmap(dpi=200).tobytes("png")` (DPI hardcoded at the call site) and feeds the PNG to `self.ocr_engine.extract_text()`. No preprocessing (denoise/threshold/deskew) beyond PyMuPDF's own rasterization. An optional NVIDIA Nemotron OCR v2 fallback exists for weak results but requires an API key not configured in this environment, so it never fired during this benchmark (confirmed not exercised: `nvidia_ocr.is_available()` path was not invoked by this script at all).
  - `backend/app/services/extraction/ocr_engine.py::TesseractOCREngine.extract_text()`: `pytesseract.image_to_data(image, output_type=Output.DICT)`, grouping words by `(block_num, par_num, line_num)`, Tesseract 5.5.3.20260724.
  - Downstream: `app/services/extraction/bill_parser.py::parse_bill_text()` - vendor/date/amount/invoice-number/GSTIN/category parsing, unchanged.
  - Page-6 diagnostic artifacts reused as-is: `reports/ocr-diagnostic-page6/dpi-matrix-results.json` (single-page DPI sweep: 96/150/200 DPI all returned 0 chars on vivek-bhai p6; 300 DPI recovered 311 chars at confidence 0.717; 400 DPI recovered 316 chars at confidence 0.735 - diminishing returns above 300). This benchmark exists to test whether that single-page finding generalizes.

## Dataset selection (documented before running)

All 12 pages below are OCR-routed (`has_selectable_text=false` at native-text stage, confirmed via `reports/page-census.json`) and have existing, unmodified human-verified ground truth in `datasets/human-verified-bill-annotations.json` (reused verbatim; two pre-existing, previously-documented ground-truth corrections carried over unchanged - see `reports/receipt-extraction-baseline-20261013.json`). Selected to cover every category the task required:

| # | File | Page | Why selected |
|---|---|---|---|
| 1 | vivek-bhai-report-ER41220.pdf | 6 | The known failing low-resolution receipt (0 chars at 200 DPI, confirmed in the prior task) |
| 2 | vivek-bhai-report-ER41220.pdf | 7 | Handwritten, low-confidence receipt - image-heavy, genuinely hard even by eye |
| 3 | vivek-bhai-report-ER41220.pdf | 9 | Clean thermal receipt, image-heavy |
| 4 | vivek-bhai-report-ER41220.pdf | 15 | Clean digital (Zoho) invoice - ground truth expected native text, but page-census shows it's actually OCR-routed (nativeTextLength=2); a genuinely surprising, not cherry-picked, inclusion |
| 5 | vivek-bhai-report-ER41220.pdf | 25 | Short transaction screenshot |
| 6 | ER-40706.pdf | 6 | Transaction screenshot (PhonePe) |
| 7 | ER-40706.pdf | 10 | Clean digital invoice, image-heavy (photographed) |
| 8 | ER-40706.pdf | 12 | Clean card-payment slip receipt |
| 9 | ER-40706.pdf | 20 | Multi-transaction credit-card statement |
| 10 | ER-40706.pdf | 22 | Multi-transaction FASTag e-statement, tinted/blurred/skewed - worst image quality in the corpus besides page 6 |
| 11 | ER-39832.pdf | 3 | Clean thermal receipt |
| 12 | ER-39832.pdf | 6 | Handwritten invoice on printed template |

A 13th page, **ER-40782.pdf page 3**, is documented as a **native-text control** and deliberately *not* rendered/OCR'd: its native text is well above the 20-char threshold (347 chars), so it never reaches the DPI-dependent code path at either setting - proven from the code itself (see Baseline above), not assumed.

Expense Report content/header pages (ER-39832 p1-2, ER-40706 p1-2, etc.) are excluded from the render/OCR loop for the same reason: they are native-text pages and are never routed to OCR, so re-rendering them at a different DPI would test nothing relevant to this benchmark.

## Results

### Field-level outcome per page (amount field, against ground truth - including the two previously-documented corrections, never amending the stored annotations file)

| File / page | 200 DPI amount | 300 DPI amount | Ground truth | Outcome |
|---|---|---|---|---|
| vivek p6 | `null` (0 chars, OCR returned nothing) | **2500.0 (correct)** | 2500.0 | **IMPROVED** |
| vivek p7 | `null` | `null` | 300 | unchanged (wrong both ways - genuinely illegible handwriting per ground truth's own note) |
| vivek p9 | `null` (0 chars) | `null` (40 chars recovered, still no amount) | 685.0 | unchanged (wrong both ways; text recovery improved but field accuracy did not) |
| vivek p15 | **1150.0 (correct)** | `null` ("Multiple differing amounts found: [1.0, 1150.0]") | 1150.0 | **WORSENED - regression** |
| vivek p25 | `null` | `null` | 500 | unchanged (wrong both ways - parser doesn't match the PhonePe "~500" format, unrelated to DPI) |
| ER-40706 p6 | `null` | `null` | 3000 | unchanged (wrong both ways - same parser-format limitation, unrelated to DPI) |
| ER-40706 p10 | 27999.92 (wrong) | 27999.92 (wrong, identical) | 34999.90 | unchanged (same pre-existing "largest unlabeled amount" mis-pick, unrelated to DPI) |
| ER-40706 p12 | **35000.0 (correct)** | **35000.0 (correct)** | 35000.0 | unchanged - correct both ways |
| ER-40706 p20 | 113824.75 (wrong) | 113824.75 (wrong, identical) | `null` (statement) | unchanged (Python has no statement-detection logic - a separate, known, out-of-scope limitation) |
| ER-40706 p22 | 31.0 (wrong) | 2500.0 (wrong, different) | `null` (statement) | unchanged classification, but DPI visibly changes *which* wrong value comes out |
| ER-39832 p3 | **2000.0 (correct)** | **2000.0 (correct)** | 2000.0 | unchanged - correct both ways |
| ER-39832 p6 | `null` (668 chars) | `null` (189 chars - noticeably less text recovered) | 220 | unchanged (wrong both ways; 300 DPI actually recovered *less* usable text on this page) |

**Net: 1 improved, 1 worsened, 10 unchanged (2 already-correct, 8 already-wrong for reasons unrelated to DPI).**

### OCR complete-failure count (0 characters returned)

- 200 DPI: **2 of 12** (vivek p6, vivek p9)
- 300 DPI: **0 of 12**

300 DPI eliminates every complete OCR failure in this sample, but that only translated into a corrected *field* value on 1 of those 2 pages (vivek p6); the other (vivek p9) went from "nothing" to "some text, still no amount."

### The regression, examined

vivek-bhai page 15 (Zoho-generated tax invoice) OCRs cleanly at both DPIs (86.6-86.7% confidence, ~1230 chars either way) - this is not a resolution problem. At 200 DPI, Tesseract misreads the "Balance Due 0.00" line as "20.00"; at 300 DPI it misreads the same line as "70.00", which OCRs further astray into a `1.0` token. `bill_parser._find_amount`'s "multiple differing amounts at the same label priority" safety check (correctly, by design) then abstains rather than guess between `1.0` and `1150.0` - at 200 DPI that extra spurious token didn't appear, so the check never triggered. This is a real, reproducible case where sharper rendering changed *how* an OCR misread, not whether misreads happen, and the knock-on effect broke an otherwise-correct extraction. It is a parser-robustness gap (a stray tiny number shouldn't out-rank a clearly bigger repeated total) worth fixing independently of this benchmark, not a reason 300 DPI is bad in general.

### ER-39832 page 6: 300 DPI recovered less text, not more

200 DPI: 668 characters, including the full declaration/signature block. 300 DPI: 189 characters - most of the lower page content is missing. Both still fail to extract the handwritten amount, so this did not change the field-level outcome recorded above, but it is direct evidence against "300 DPI is strictly more text" as a general assumption (Tesseract's own page-segmentation analysis evidently behaved differently at the larger pixel dimensions for this specific layout).

### Timing (render + OCR, Tesseract `image_to_data`, same engine/config both runs)

| | 200 DPI | 300 DPI | Ratio |
|---|---|---|---|
| Total (12 pages, render+OCR) | 29.03 s | 41.32 s | **1.42x** |
| Median per page (render+OCR) | 1.71 s | 2.45 s | 1.43x |
| Median render only | 347 ms | n/a (not separately summarized; see raw data) | - |
| Median OCR only | 1.40 s | n/a (see raw data) | - |

Cost scales with page size, not just DPI: the two largest-dimension pages in the sample (ER-40706 p20/p22, already 3534x5000px at 200 DPI - a non-standard, oversized scan) took 6.2s and 5.4s respectively at 200 DPI, rising to 9.3s and 8.7s at 300 DPI - roughly +3-4 seconds *each*, the single biggest absolute cost increases in the sample. Rendered pixel count (and therefore raw bitmap memory before PNG compression) grows with `(300/200)^2 = 2.25x` per OCR'd page at 300 DPI, consistent with the measured ~1.4x wall-clock increase once PNG encoding/Tesseract overhead is factored in (not a 1:1 relationship, since fixed per-call overhead doesn't scale with pixels).

### Native-text routing (not affected by DPI, confirmed by code inspection - see Baseline)

ER-40782 p3 never reaches the DPI-dependent branch at either setting; this is a structural property of `has_selectable_text`'s definition, not something that needed empirical re-testing per page.

## Recommendation

**Do not switch the default OCR render DPI to 300 globally.** The evidence does not support "higher DPI is automatically better" (per the task's own caution): across 12 real pages, 300 DPI produced exactly one win and one loss on the one field with full ground truth coverage (amount), plus one case of a clean digital invoice regressing and one case of less text being recovered overall. The ~42% total processing-time increase is real and falls disproportionately on already-large scanned pages.

**Recommended: a targeted fallback that retries at 300 DPI only when the 200 DPI OCR result is completely empty** (`len(result["text"].strip()) == 0`), i.e. exactly the two cases this benchmark found at 200 DPI (vivek p6, vivek p9) and the one the prior page-6 task diagnosed. This option:

- Captures the only unambiguous win in this sample (vivek p6: `null` to correct `2500.0`) and the partial-recovery case (vivek p9: 0 to 40 chars, still no amount but strictly no worse).
- **Structurally cannot cause the vivek p15 regression or the ER-39832 p6 text-loss case**, because both of those pages already returned non-empty text at 200 DPI and would never trigger a retry under this rule - the regression is confined to pages that are never touched by this narrower change.
- Adds processing cost only on the rare pages that already return nothing useful at 200 DPI, so the ~42% total-time cost measured here does not generalize to production traffic (most pages are not empty at 200 DPI).

A broader "retry below confidence X" trigger is **not** supported by this data: vivek p7 (confidence 0.27-0.29, genuinely illegible handwriting per its own ground-truth note) and vivek p25 (confidence 0.74-0.80, already reasonable) would both be swept into a confidence-based retry without benefit - p7 stayed unreadable at 300 DPI too, and p25's amount-extraction gap is a parser format issue, not a resolution issue. The narrower "empty-text-only" trigger is the one rule directly and fully justified by evidence in this benchmark.

## Remaining risks / limitations

- Sample size is 12 OCR'd pages (plus 1 native control) - small enough that a single page's quirk (vivek p15's regression) swings the field-level tally by one. The *direction and mechanism* of each finding (not just the count) is what the recommendation above rests on.
- Ground truth exists only for one field (`amount`, with incidental vendor/invoice-number notes) per page; vendor-field and invoice-number-field accuracy at each DPI were not separately scored (out of scope for this task, which centers on amount correctness and OCR legibility/cost).
- `renderMs`/`ocrMs` were measured via `time.perf_counter()` around single, uncontended calls on this machine; absolute values will vary by hardware/load, though the **relative** ~1.4x ratio between DPIs should be stable since it reflects pixel-count scaling, not machine-specific overhead.
- This benchmark did not exercise the optional NVIDIA Nemotron OCR v2 fallback path (`text_extraction.py`'s `nvidia_ocr` branch) at either DPI, since it requires external network access/an API key not configured here - consistent with the "no external AI uploads" safety rule. Its interaction with DPI, if ever enabled, is untested.

## Changed files (all new, nothing modified)

- `experiments/node-extraction/benchmarks/dpi-200-vs-300/run_benchmark.py` (new benchmark script)
- `experiments/node-extraction/benchmarks/dpi-200-vs-300/results-raw.json` (new, raw per-page/per-DPI data)
- `experiments/node-extraction/benchmarks/dpi-200-vs-300/REPORT.md` (this report)

**No production file was modified.** `backend/` is untouched by this task (the pre-existing modified files shown in `git status` predate this task and are unrelated to it). The Node experiment's `src/` files were not modified. The production database, UAT data, original reference PDFs, existing baseline reports (`reports/*.json`), and `datasets/human-verified-bill-annotations.json` were read-only inputs and are unchanged. Nothing was staged or committed.

**No production change has been made.** Stopping here per the task's instruction, pending authorization for any of: (a) 300 DPI globally, (b) 300 DPI for identified low-resolution cases, or (c) the empty-result retry fallback recommended above.
