// Port of parse_amount from backend/app/services/extraction/money.py.
// Never guesses: returns null if the text has no recognizable number.

const CURRENCY_PREFIX = /^\s*(?:₹|rs\.?|inr)\s*/i;
const NUMBER_RE = /-?\(?\d[\d,]*(?:\.\d+)?\)?/;

export function parseAmount(text) {
  if (!text) return null;
  const cleaned = text.trim().replace(CURRENCY_PREFIX, "");
  const match = NUMBER_RE.exec(cleaned);
  if (!match) return null;
  let token = match[0];
  const isNegative = token.startsWith("(") && token.endsWith(")");
  token = token.replace(/[()]/g, "").replace(/,/g, "");
  const value = parseFloat(token);
  if (Number.isNaN(value)) return null;
  return isNegative ? -value : value;
}

// Port of amounts_match from money.py: an employee commonly enters a bill's
// total rounded/truncated to a whole rupee rather than its exact decimal
// figure, in EITHER direction - for a bill with decimals, both neighbouring
// whole rupees are valid (Rs.4,499.77 accepts 4499 OR 4500, nothing else).
// For an already-whole-rupee bill, floor and ceiling coincide, so only that
// exact value is accepted. Values are rounded to cents first (JS has no
// native Decimal) to avoid the same float-representation artifacts the
// Python port guards against via Decimal(str(...)).
export function amountsMatch(claimed, billAmount) {
  if (claimed === null || claimed === undefined || billAmount === null || billAmount === undefined) return false;
  const c = Math.round(claimed * 100) / 100;
  const b = Math.round(billAmount * 100) / 100;
  if (c === b) return true;
  return c === Math.floor(b) || c === Math.ceil(b);
}
