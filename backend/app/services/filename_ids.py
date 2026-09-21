"""Best-effort ER/Trip ID recognition from the uploaded filename only.

This reads no PDF content, so it is not "extraction" - it's a filename
convention already used by real sample documents (e.g. "ER-39832.pdf",
"TRIP-24935_Trip_To_Indore.pdf"). When the pattern isn't present, the
field is left null rather than guessed.
"""

import re

ER_PATTERN = re.compile(r"\bER-(\d+)", re.IGNORECASE)
TRIP_PATTERN = re.compile(r"\bTRIP-(\d+)", re.IGNORECASE)


def extract_er_id(filename: str) -> str | None:
    match = ER_PATTERN.search(filename)
    return f"ER-{match.group(1)}" if match else None


def extract_trip_id(filename: str) -> str | None:
    match = TRIP_PATTERN.search(filename)
    return f"TRIP-{match.group(1)}" if match else None
