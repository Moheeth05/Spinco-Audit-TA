# Real statement-page text fixtures

Real Node-OCR'd text (tesseract.js), captured exactly as extracted, from the two pages that confirmed this bug in `reports/receipt-extraction-baseline-20261013.json`:

- `credit-card-statement-ER-40706-p20.txt` — `fixtures/reference-pdfs/ER-40706.pdf` page 20 (ICICI Amazon Pay Credit Card statement). Ground truth: `amount` should be `null` (no single bill total exists on this page). Before this fix, Python extracted `113824.75` (the card's outstanding balance) here.
- `fastag-statement-ER-40706-p22.txt` — `fixtures/reference-pdfs/ER-40706.pdf` page 22 (ICICI FASTag E-Statement). Ground truth: `amount` should be `null`. Before this fix, Python extracted `31` (a garbled OCR fragment) here.

Saved as text so the regression tests run instantly and deterministically without re-running OCR every time. The source PDFs themselves (`fixtures/reference-pdfs/`) are untouched.
