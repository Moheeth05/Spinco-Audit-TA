# Research report: sources of Indian fuel-receipt examples (concise)

Full per-source detail, retrieval dates and licence text: `sources.json`. Method: page text (WebFetch) and search snippets only; **nothing was downloaded or stored from any external source and no receipt image or OCR text was sent anywhere**. "Verified" means read on the page; "snippet" means only seen in a search result.

## Findings

1. **No public dataset of authentic Indian petrol/diesel receipts exists in anything I could find** (Wikimedia Commons, Hugging Face, Kaggle, Roboflow, GitHub, academic datasets; two rounds of searching, 2026-10-20 and 2026-10-22).
2. **Public Indian fuel-receipt material is bill generators**, which produce *simulated* receipts: OnlineBiller and BillCopy offer BPCL, HPCL, IndianOil, Nayara, Jio-bp (and Shell/Essar logos on some) templates with a dealer name under the brand, an `ORIGINAL` line, a glued `30-JUN-202618:42:00` style date and `AMOUNT ... INR` wording, and a "crumpled" variant. They state no licence for the generated documents or logos. They were used **only** to understand layout; nothing was copied and no generated receipt was labelled authentic. `fuelbill.in` was unreachable (HTTP 530).
3. **Generic receipt datasets are not Indian and not fuel-specific:** SROIE (Malaysian shop receipts; the licence on one Hugging Face mirror says CC-BY-4.0 but the original terms could not be found, so it is *not* verified), CORD (Indonesian shop/restaurant receipts, CC-BY-4.0 per its page), ReceiptSense/CORU (Arabic-English, MIT per snippet), AMuRD (Arabic-English), a Humans-in-the-Loop set (192 generic images, CC0 per its page, form-gated, country and receipt types not stated). None was downloaded.
4. **Outlet data, not receipts:** `ForceGT/india-fuel-pumps` (MIT; ~103,000 HPCL/IOCL/BPCL/Jio-bp/Nayara/Shell outlets). A possible *future supporting vocabulary* for checking a read pump name; deliberately not used so the extractor stays a pure reader of the receipt.
5. **Hosted receipt-OCR APIs exist** (e.g. a commercial petrol-receipt API) but would require uploading receipts to an external service, which is prohibited here.

## Consequences

* The only authentic receipts available are 22 physical receipts embedded in the project's own approved reports (BPCL 12, HP 6, IndianOil 4); all other brands are exercised with **synthetic** receipts only, kept strictly separate from authentic scores.
* Authentic receipts of Nayara, Jio-bp, Shell and independent stations must come from an authorized person through `intake/INTAKE.md` (provenance, permitted usage, family reserved as held-out, human-verified labels).
* Licence note: a publicly reachable file is not thereby reusable; none of the above was stored, modified or redistributed.
