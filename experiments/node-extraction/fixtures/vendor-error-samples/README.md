# Real vendor-error text fixtures

Real Node-OCR'd (or native, where noted) text, captured exactly as extracted via `scripts/diagnose-vendor-errors.mjs`, from the 5 pages confirmed in `reports/receipt-extraction-baseline-20261013.json` to pick a non-vendor candidate (a UI label, status banner, document-type heading, customer-side metadata caption, or disclaimer boilerplate) instead of the real vendor/payee.

OCR text is not perfectly deterministic between runs (tesseract.js can produce slightly different output on repeat invocations of the same image), so these snapshots are saved as fixtures rather than re-OCR'd on every test run — both for test speed and so the regression tests check the exact confirmed failure text, not a fresh and possibly-different OCR pass.

- `vivek-bhai-p25-phonepe.txt` — PhonePe payment screenshot ("Paid to" label, payee PANKAJ BALDEVBHAI PARMAR on the next line).
- `er-40706-p6-phonepe.txt` — Another PhonePe screenshot ("Transaction Successful" banner, payee Mr RAM MURTI two lines down).
- `er-40706-p10-hotel-invoice.txt` — Home Town Resort hotel invoice (customer-side "Party's GSTIN : ... Place of Service" caption, real vendor "Home Town RESORT" in the letterhead above it).
- `er-40706-p12-card-slip.txt` — A card-swipe confirmation slip (no reliably identifiable vendor — heavily garbled OCR of a generic bank-terminal disclaimer; used as the "insufficient evidence, must abstain" case, not a "find the hidden vendor" case).
- `er-40782-p3-paytm.txt` — Paytm payment-success screenshot, native PDF text (not OCR) — bare "Name" field label, real vendor "CNG Mother Station" (OCR'd/rendered as "CNG Mather Station") further down.
