# Adding authentic receipts (intake) - for the project owner / an authorised person

Why this exists: **no public dataset of authentic Indian petrol receipts was found** (see `../sources.json`), and every receipt in the project's own reports has already
been used. The brands that matter most and are missing - **Nayara, Jio-bp, Shell and independent stations** - can therefore only come from an authorised person.

## What may be added
Only genuine issued receipts (a photo or scan of a printed receipt) for which someone can state the source, the owner, the permission basis and the permitted usage.
Not accepted: bill-generator or template output, synthetic receipts, anything you are not authorised to store and evaluate, anything you would have to upload to an external service.

## Steps
1. Copy `provenance_template.json`, fill every field (one provenance file per image; the same text can be reused for a batch from one source).
2. Decide the **split by layout family** before looking at results: `development` (the parser may be changed using these) or `heldout_untouched`
   (never used to change the parser). A family goes wholly to one side; the tool refuses a family that is already on the other side.
   Recommended: for each priority brand put at least one *whole family* in `heldout_untouched`.
3. Register: `node scripts/register_intake.mjs <image> --provenance <file.json> --split development|heldout_untouched`
   - refuses missing provenance, generator/synthetic sources, uses that may not be stored/evaluated, exact duplicates, and split-conflicting families;
   - stores the image under `corpus/authentic_intake/pending/`, adds it to `intake/intake_register.json` as `pending_human_label` with **no label**.
4. Label it by an authorised human only: build a blind pack (`node scripts/build_review_pack.mjs --scope pending`), have the reviewer fill it in offline, then
   `node scripts/apply_verification.mjs <export.json> [<second reviewer's export.json>]`. See `../verification/README.md`. Until then the receipt is run through the extractor
   (if you want) but is **never scored**.
5. Lock the held-out set to the current extractor: `node scripts/lock_heldout.mjs`. `run_eval` then reports, every time, whether the extractor has changed since the lock;
   if it has, the held-out set is **development data** and must be reported as such.
   - The lock is created **only** if the whole register is valid (no malformed entry, duplicate id / image path / hash, or family on both splits), there is at least one `heldout_untouched` entry, and every
     held-out entry's image is available and its SHA-256 matches the register. Anything else: the command exits 1, says why, and writes nothing.
   - An existing `intake/heldout_lock.json` is **never** overwritten, repaired or deleted by the script (it refuses and leaves it untouched). There is no built-in procedure for replacing a lock; whether and how to retire one is a
     decision for the project owner, not something the script does. Lock only when the held-out set is complete: an empty lock is refused.
6. Evaluate once, at the end: `node scripts/run_eval.mjs --impl improved --label <name>` (groups `authentic_intake_dev` and `authentic_intake_heldout`; only human-verified receipts are scored).
   - Before any OCR, `run_eval` checks that the register is valid, every selected image is available (intake images also against their registered SHA-256), and - if held-out intake receipts are selected - that a valid
     held-out lock exists and agrees with the register (ids, hashes, family, brand). If anything is wrong it exits 1, says why, and writes no report. See `../verification/README.md`.

## Rules that never bend
* No receipt image or OCR text is sent to any external service. NVIDIA / DeepSeek stay disabled.
* A label is "verified" only when `apply_verification.mjs` wrote it from an authorised reviewer's export (reviewer id + authorization confirmed).
* Report authentic and synthetic results separately, with numerators and denominators; never merge them.
* A set used to change the parser is development data, whatever it was called before.
