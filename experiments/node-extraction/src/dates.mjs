// Port of backend/app/services/extraction/dates.py - same patterns, same
// DD/MM-preferred-with-corroboration-fallback logic for 2-digit years.
// Never guesses: returns [] when nothing matches.

const MONTH_NAMES = {
  jan: 1, january: 1,
  feb: 2, february: 2,
  mar: 3, march: 3,
  apr: 4, april: 4,
  may: 5,
  jun: 6, june: 6,
  jul: 7, july: 7,
  aug: 8, august: 8,
  sep: 9, sept: 9, september: 9,
  oct: 10, october: 10,
  nov: 11, november: 11,
  dec: 12, december: 12,
};
const MONTH_ALT = Object.keys(MONTH_NAMES).sort((a, b) => b.length - a.length).join("|");

const DATE_PATTERN = /\b(\d{1,2})[/-](\d{1,2})[/-](\d{4})\b/g;
// ISO 8601 (YYYY-MM-DD) - mirrors backend/.../dates.py's _DATE_PATTERN_ISO,
// added this session for a real fuel-pump receipt ("DATE: 2026-09-23").
// Unambiguous vs DATE_PATTERN (leading 4-digit year can never match that
// pattern's 1-2 digit day/month start) - no DD/MM-style ambiguity.
const DATE_PATTERN_ISO = /\b(\d{4})-(\d{1,2})-(\d{1,2})\b/g;
const DATE_PATTERN_SHORT_YEAR = /\b(\d{1,2})[/-](\d{1,2})[/-](\d{2})\b/g;
const MONTH_DAY_YEAR_RE = new RegExp(`\\b(${MONTH_ALT})\\s+(\\d{1,2}),?\\s+(\\d{4})\\b`, "gi");
const DAY_MONTH_YEAR_RE = new RegExp(`\\b(\\d{1,2})[\\s-](${MONTH_ALT})[\\s-](\\d{4})\\b`, "gi");

function safeDate(year, month, day) {
  if (month < 1 || month > 12 || day < 1 || day > 31) return null;
  const d = new Date(Date.UTC(year, month - 1, day));
  if (d.getUTCFullYear() !== year || d.getUTCMonth() !== month - 1 || d.getUTCDate() !== day) return null;
  return { year, month, day, iso: `${year}-${String(month).padStart(2, "0")}-${String(day).padStart(2, "0")}` };
}

function dateKey(d) {
  return d.iso;
}

export function findAllDates(text) {
  text = text || "";
  const found = []; // {pos, date}
  const strongDates = new Set();

  for (const m of text.matchAll(DATE_PATTERN)) {
    const day = parseInt(m[1], 10), month = parseInt(m[2], 10), year = parseInt(m[3], 10);
    const d = safeDate(year, month, day);
    if (d) {
      found.push({ pos: m.index, date: d });
      strongDates.add(dateKey(d));
    }
  }

  for (const m of text.matchAll(DATE_PATTERN_ISO)) {
    const year = parseInt(m[1], 10), month = parseInt(m[2], 10), day = parseInt(m[3], 10);
    const d = safeDate(year, month, day);
    if (d) {
      found.push({ pos: m.index, date: d });
      strongDates.add(dateKey(d));
    }
  }

  for (const m of text.matchAll(MONTH_DAY_YEAR_RE)) {
    const month = MONTH_NAMES[m[1].toLowerCase()];
    const day = parseInt(m[2], 10), year = parseInt(m[3], 10);
    const d = safeDate(year, month, day);
    if (d) {
      found.push({ pos: m.index, date: d });
      strongDates.add(dateKey(d));
    }
  }

  for (const m of text.matchAll(DAY_MONTH_YEAR_RE)) {
    const day = parseInt(m[1], 10);
    const month = MONTH_NAMES[m[2].toLowerCase()];
    const year = parseInt(m[3], 10);
    const d = safeDate(year, month, day);
    if (d) {
      found.push({ pos: m.index, date: d });
      strongDates.add(dateKey(d));
    }
  }

  for (const m of text.matchAll(DATE_PATTERN_SHORT_YEAR)) {
    const a = parseInt(m[1], 10), b = parseInt(m[2], 10), yy = parseInt(m[3], 10);
    const year = 2000 + yy;
    const primary = safeDate(year, b, a); // DD/MM preferred
    const alt = a >= 1 && a <= 12 ? safeDate(year, a, b) : null; // MM/DD fallback

    if (primary === null && alt !== null) {
      found.push({ pos: m.index, date: alt });
    } else if (
      primary !== null &&
      !strongDates.has(dateKey(primary)) &&
      alt !== null &&
      strongDates.has(dateKey(alt))
    ) {
      found.push({ pos: m.index, date: alt });
    } else if (primary !== null) {
      found.push({ pos: m.index, date: primary });
    }
  }

  found.sort((x, y) => x.pos - y.pos);
  return found.map((f) => f.date);
}

export function parseDate(text) {
  const dates = findAllDates(text);
  return dates.length ? dates[0] : null;
}
