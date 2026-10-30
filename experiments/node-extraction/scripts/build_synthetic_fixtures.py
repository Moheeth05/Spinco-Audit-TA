"""Builds synthetic test PDFs for structural variations NOT present in the
real reference corpus (checked against datasets/annotations.jsonl's real
field-order/content combinations first). Python/fitz is used purely as a
fixture-authoring tool here - it plays no part in the Node.js pipeline
under test, which consumes these PDFs exactly like any other file.
"""
import fitz

OUT_DIR = r"D:\Bills\experiments\node-extraction\fixtures\synthetic"
import os
os.makedirs(OUT_DIR, exist_ok=True)


def build(path, header_lines, row_blocks):
    doc = fitz.open()
    page = doc.new_page()
    y = 72
    for line in header_lines:
        page.insert_text((72, y), line, fontsize=11)
        y += 16
    for block in row_blocks:
        for line in block:
            if y > 760:
                page = doc.new_page()
                y = 72
            page.insert_text((72, y), line, fontsize=10)
            y += 14
    doc.save(path)
    doc.close()
    print(f"built {path}")


# Case: optional-field order never seen in the real corpus - every real
# report always prints Merchant before Ref# before Attendees (when present
# at all); this reverses that order AND adds a never-seen "Start & End KM"
# placement after the category instead of before it.
build(
    f"{OUT_DIR}\\field_order_variant.pdf",
    [
        "Expense Report", "ER-SYN-001", "Amount to be Reimbursed", "Rs.500.00",
        "Report", "October", "Submitted By", "SYNTH USER", "synth@spincotech.com",
        "Submitted On", "01/10/2026", "EXPENSE SUMMARY",
        "S.No", "Expense Details", "Category", "Amount (INR)",
    ],
    [
        [
            "1.", "01/10/2026",
            "Attendees : A Synthetic Person",
            "Ref# : SYNRF001",
            "Merchant : Synth Vendor Co",
            "LOCAL CONVEYANCE",
            "Start & End KM : 12",
            "synthetic test row with reversed field order",
            "Rs.500.00",
        ],
        [
            "REPORT SUMMARY BY CURRENCY",
            "TOTAL", "INR",
            "Total Expense Amount", "500.00",
            "Total Reimbursable Amount", "Rs.500.00",
        ],
    ],
)

# Case: category text with OCR-style noise (never occurs in these native-
# text PDFs - only matters once an OCR-sourced page is fed through the
# same parser). Confirms is_policy_master_category's normalize step still
# rejects a near-miss rather than silently accepting it, matching the
# existing deliberate design (no fuzzy category matching in the Expense
# Report parser, unlike the bill parser's fuzzy fallback).
build(
    f"{OUT_DIR}\\ocr_noisy_category.pdf",
    [
        "Expense Report", "ER-SYN-002", "Amount to be Reimbursed", "Rs.300.00",
        "Report", "October", "Submitted By", "SYNTH USER TWO", "synth2@spincotech.com",
        "Submitted On", "01/10/2026", "EXPENSE SUMMARY",
        "S.No", "Expense Details", "Category", "Amount (INR)",
    ],
    [
        [
            "1.", "02/10/2026",
            "BATTA",  # clean - control row, should resolve
            "control row",
            "Rs.150.00",
        ],
        [
            "2.", "02/10/2026",
            "BATTAA",  # OCR-style noise: one extra character - must NOT resolve
            "noisy category row - should stay unresolved, never guessed",
            "Rs.150.00",
        ],
        [
            "REPORT SUMMARY BY CURRENCY",
            "TOTAL", "INR",
            "Total Expense Amount", "300.00",
            "Total Reimbursable Amount", "Rs.300.00",
        ],
    ],
)

print("done")
