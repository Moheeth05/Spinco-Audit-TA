# Node/npm extraction experiment — how far can it go?

**Status: experimental, read-only reference. Never wired into the production backend or Electron app. The real application's extraction pipeline remains 100% Python.**

**No ML training or fine-tuning occurred anywhere in this experiment.** Every improvement described below is deterministic, explainable logic — regex/string rules and coordinate-aware PDF reconstruction with measured threshold tuning — not a trained model. If that framing changes in a future round, this line will be updated to say so explicitly and name the model.

## Headline result

For the recurring Spinco Expense Report format (the 6 real reports this experiment had available — see Scope below), the full Node.js pipeline — pdfjs-dist text extraction feeding the Node business-rule parser, no Python involved anywhere in the JS path — reaches **100% field-level accuracy**: 154/154 expense rows, every field (date, category, merchant, reference ID, amount), across all 6 reports, run fresh via `scripts/evaluate-parity.mjs`, **including the 3-report held-out set that was never inspected while designing the fix.**

This supersedes the previous round's finding ("PyMuPDF does internal line reconstruction pdfjs-dist has no equivalent for") — that diagnosis was correct as far as it went, but incomplete: a combination of three pdfjs-dist-level signals turned out to replicate PyMuPDF's actual line segmentation exactly, for this document family. See "What changed" below.

## Reference dataset

6 of the 7 originally-named real Expense Reports were found on disk (4 at the repo root, 2 already uploaded into `uat-data-test` — read only, never modified); `dipak report.pdf` was not found anywhere and is recorded as genuinely unavailable in `datasets/manifest.json`, not substituted or fabricated. Split by report (not by row, to avoid leakage between similar rows in the same document) into:

- **Dev (3 reports)**: `reahul-report-ER40513.pdf` (28 rows), `ER-40706.pdf` (32 rows), `ER-39832.pdf` (12 rows) — used to diagnose the line-merging bug and tune the fix.
- **Held-out (3 reports)**: `ER-40223.pdf` (17 rows), `ER-40782.pdf` (7 rows + embedded receipt pages), `vivek-bhai-report-ER41220.pdf` (58 rows) — read only for the single final evaluation pass, after the reconstruction approach was frozen. No threshold was adjusted based on a held-out result.

`datasets/manifest.json` records each file's SHA-256, page count, and role. `datasets/annotations.jsonl` holds the real per-row ground truth (from the Python oracle, with 2 rows additionally hand-verified against this conversation's own rendered PDF page images). `datasets/splits.json` documents the split rationale.

## What changed: the text-extraction fix

Diagnosed by inspecting pdfjs-dist's raw `getTextContent()` items directly (`scripts/diagnose-row-merge.mjs`) against the real ER-40513 document, rather than guessing. Three independent signals, verified against the actual item stream, each catching line breaks the others miss — `src/textExtraction.mjs`'s `linearTextFromItems`:

1. **pdfjs-dist's own `hasEOL` flag** — a content-stream-native line-break marker, including on empty-string items that carry no text (the previous version discarded those before ever checking the flag, silently losing real signal).
2. **A Y-position jump** beyond 5pt from the previous item — needed because a horizontal multi-cell summary strip (e.g. "Total Expense" / "( - )" / "Non Reimbursable" packed together) has no `hasEOL` between cells, but PyMuPDF still puts each on its own line. (Originally 3pt; widened to 5pt after a real confirmed case — a `[1]` policy-violation superscript marker sitting 3.48pt off its own category text's baseline — was being wrongly split at 3pt, while the genuine summary-strip breaks measure ~6pt, still safely caught.)
3. **An anomalously wide whitespace-only item** (`width > 15`) — the PDF generator renders a table *column* gap as a single stretched " " item (confirmed real values: 31.5pt, 140pt) rather than real inter-word spacing (never more than a couple of points in this corpus). This is what signals 1 and 2 both miss on a short row with no wrapped detail line, where S.No/date/category all land on one content-stream line at one shared Y.

No single signal matches PyMuPDF's line segmentation; their union does.

## Remaining known gaps (honestly reported, not hidden)

- **Cosmetic double-vs-single internal space**, within free-text prose (never at a field boundary): confirmed on `ER-40706.pdf` and `vivek-bhai-report-ER41220.pdf`. PyMuPDF renders certain PDF-internal space-width adjustments as two literal space characters; this reconstruction emits one. Does not affect any structured field (the parser splits/trims lines, so `"Friend home"` vs `"Friend  home"` parses identically) — confirmed by the 100% field-level result above, which includes exactly these rows.
- **Embedded receipt/bill pages** (scanned fuel receipts, OCR'd stamps) are explicitly out of scope — this round's fix targets the Expense Report's own header/table/footer structure, not supporting-bill image content, per the task's own scope guidance. `ER-40782.pdf`'s 8 receipt pages show expected mismatches; its 2 actual Expense Summary pages are a 100% match.
- **OCR path** (`src/ocrEngine.mjs`, tesseract.js): wired but not evaluated this round — no scanned/image-only page was built and run through it. Flagged, not silently skipped.
- Only 6 real reports were available. The acceptance of "generalizes to unseen reports of the same format" rests on 3 held-out reports reaching the same 100% result as the 3 dev reports — a meaningful signal, not a large-sample guarantee. More real reports would strengthen this further.

## Synthetic fixtures (`fixtures/synthetic/`, synthetic-only — not proof of unseen real-world layouts)

- `field_order_variant.pdf` — optional-field order never seen in the real corpus (Attendees → Ref# → Merchant, reversed from every real report's own order). Matches Python exactly.
- `ocr_noisy_category.pdf` — a category name with a single injected extra character (`BATTAA` vs `BATTA`). Confirms the Node parser, like the Python original, leaves this `category: null` rather than fuzzy-guessing it — the Expense Report parser has no fuzzy fallback (unlike the bill parser's deliberately narrow one), and the port preserves that.

## Business-rule parity (unchanged, reconfirmed after the fix — zero regression)

| Layer | Parity |
|---|---|
| Dates, money, 29-category vocabulary | 49/49 |
| Full bill parser | 100/100 |
| Text-extraction heuristics (pure logic) | 10/10 |
| **End-to-end Expense Report extraction (real pipeline, all 6 reports)** | **154/154 rows, every field** |

Run `node parity-test.mjs && node parity-test-billparser.mjs && node parity-test-textextraction.mjs && node scripts/evaluate-parity.mjs` to reproduce everything above.

## Scope not covered

`bill_matcher.py`, `validation/checks.py`, `exception_engine.py` (matching/policy/exception business logic) and `bill_grouping.py` (multi-page bill grouping) remain out of scope — the task has consistently been PDF detail extraction. `bill_grouping.py` is pure logic (like the rules layer) and would likely port with similarly high parity now that the text-extraction layer it depends on is this much more reliable.
