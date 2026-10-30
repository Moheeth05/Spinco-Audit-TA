# Evidence for the receipt-family-specific bill-number month rule (generated)

n is small, all receipts come from three reports (ER-39832, ER-40706, ER-40513) and a few printers; the labels used in check (b) are provisional. This is evidence that the convention holds in THESE examples, not proof that it holds generally.

* authentic receipts examined: 22; OCR read the `<Mon>-<digits>-ORGNL` signature on 4.
* **(a) OCR-only check** (receipts whose OCR'd date is unambiguous, so no label is involved): 2/2 agree.
* **(b) against provisional labels**: 4/4 agree.
* receipts where the extractor actually used the rule to resolve an otherwise ambiguous date: auth_ER-39832_p04, auth_ER-40513_p06.
* family (`orgnl_physical_receipt`) receipts where OCR did not read the signature (rule silent; an ambiguous date stays unresolved): auth_ER-40223_p21, auth_ER-40706_p17, auth_ER-40706_p18, auth_ER-40513_p22.

| receipt | family | signature OCR'd | token month | OCR date (unambiguous) | provisional label date | rule used |
|---|---|---|---|---|---|---|
| auth_ER-39832_p04 | orgnl_physical_receipt | Jun-553819-ORGNL | 6 | - | 2026-06-04 | true |
| auth_ER-39832_p08 | orgnl_physical_receipt | Jun-562111-ORGNL | 6 | 2026-06-16 | 2026-06-16 | false |
| auth_ER-39832_p13 | orgnl_physical_receipt | Jun-570529-ORGNL | 6 | 2026-06-26 | 2026-06-26 | false |
| auth_ER-40223_p21 | orgnl_physical_receipt | - | - | 2026-07-31 | 2026-07-31 | false |
| auth_ER-40706_p17 | orgnl_physical_receipt | - | - | 2026-07-20 | 2026-07-20 | false |
| auth_ER-40706_p18 | orgnl_physical_receipt | - | - | 2026-07-24 | 2026-07-24 | false |
| auth_ER-40513_p06 | orgnl_physical_receipt | Sep-894177-ORGNL | 9 | - | 2026-09-02 | true |
| auth_ER-40513_p22 | orgnl_physical_receipt | - | - | 2026-09-18 | 2026-09-18 | false |

**Scope of the rule:** only the exact signature (a month abbreviation, 3+ digits, the literal `ORGNL`, on a Bill/INV/Invoice/Receipt line). It only selects between day/month readings already present in a printed numeric date; it never supplies a missing day, month or year, never overrides an unambiguous date, and is silent (date stays unresolved, review required) when the month agrees with neither or both readings. It is not a general date rule and is not applied to any other receipt family.
