// System-event timestamps (uploaded_at, created_at, verified_at, last_login_at,
// last_updated, audit log timestamps) are always serialized by the backend as
// UTC-aware ISO strings (see backend/app/schemas/common.py's UTCDateTime) -
// `new Date(isoString)` already parses those correctly since a timezone-aware
// ISO string (with a "+00:00"/"Z" suffix) is unambiguous. What was missing is
// display: by default `.toLocaleString()` renders in whatever timezone the
// browser/OS is set to, which may not be the user's actual working timezone
// (and previously masked the fact that pre-fix payloads had no offset at all,
// so the browser silently treated a UTC instant as if it were already local).
// These helpers force display to Asia/Kolkata explicitly, per the desired
// architecture - never used for business dates (expense_date, bill_date,
// submitted_on, start_date/end_date, report_month, Policy Master
// effective_date, etc.), which are calendar dates the source document states
// verbatim and must never be timezone-shifted.
const IST_ZONE = 'Asia/Kolkata'

export function formatIST(value, options) {
  if (!value) return '-'
  const d = new Date(value)
  if (Number.isNaN(d.getTime())) return '-'
  return d.toLocaleString('en-IN', { timeZone: IST_ZONE, ...options })
}

export function formatISTDate(value) {
  return formatIST(value, { year: 'numeric', month: 'short', day: 'numeric' })
}

export function formatISTDateTime(value) {
  return formatIST(value, {
    year: 'numeric',
    month: 'short',
    day: 'numeric',
    hour: '2-digit',
    minute: '2-digit',
    second: '2-digit',
  })
}
