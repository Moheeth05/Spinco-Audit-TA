# Generalization benchmark

Measures how well extraction and bill matching work on Expense Reports the code was **not** written
against. Synthetic reports with exact ground truth are rendered to real PDFs and pushed through the real
upload -> extraction -> matching -> validation pipeline (throwaway DB and storage, providers disabled,
`httpx.post` replaced by a counter that fails), then the stored results are scored.

```
cd backend
.venv\Scripts\python.exe benchmarks\run_generalization_benchmark.py --family dev heldout heldout2 heldout3 heldout4 --seed 1 2 3 --label run
```

Files: `tests/synthetic_reports.py` (receipt templates + ER layouts), `tests/synthetic_cases.py` (20 labelled
scenarios x families), `benchmarks/scoring.py` (metrics), `tests/test_generalization_benchmark.py` (the same
cases as a pytest regression guard).

## Families - and why numbers are only comparable within their discipline

| family | role |
|---|---|
| `dev` | built and tuned against |
| `heldout` | first held-out set; used for diagnosis, so no longer held out |
| `heldout2` | written after tuning on dev/heldout; **first look** exposed dotted `DD.MM.YYYY` and `DD-Mon-YY` dates |
| `heldout3` | defined before its results were seen; **first look** exposed `14Jun26`, `14/Jun/2026`, serial-last rows |
| `heldout4` | defined before its results were seen; **run once, not tuned** (apart from two fixes made after the first look: explicit "Paid to X" recipients and `YYYY/MM/DD`; those two classes are therefore not unbiased) |

A family stops being held out the moment a result from it informs a code change. The honest unbiased
numbers are the *first-look* figures; the final table also shows post-fix figures and says which is which.

## What it measures

Claim-row precision/recall (all of date, category, amount must match); receipt amount, date, merchant
(extracted as vendor/description, not merely present in the text), payment status/time/reference;
claim->bill allocation; **false-confident match rate** (a wrong bill matched, or an unsupported claim
verified); false duplicate flags; whether duplicate/unsupported claims are left unverified; expected
exceptions; review rate and whether each review was justified; time; provider-call count.

## What it cannot measure

* Real handwriting, photographs, skew/blur, scanner noise: receipts are rendered text (a few as pictures
  read by local Tesseract). **No handwriting accuracy claim can be made from this benchmark.**
* Anything the generator cannot express. 100% here means "100% of what this generator produces".
* Real-world ground truth: real reports (ER-38784, ER-41115, ...) have no complete hand-labelled truth, so
  they are compared before/after, not scored.

## Known unsupported inputs (measured, deliberately not hidden)

* ER layout with **one label per line** per claim (`Sr No:` / `Date:` / `Category:` / ... on separate
  lines): no rows are read - and none are invented (a unit test pins this).
* Bank-alert SMS text: the account-balance figure can be mistaken for the transaction amount; the
  `UPI/P2M/<ref>/<name>` reference and "debited" status are not read.
* Dotted dates with a 2-digit year (`14.06.26`) are refused (collide with decimals/versions).
* Amount in words is used only to check a printed figure, never to create one.
