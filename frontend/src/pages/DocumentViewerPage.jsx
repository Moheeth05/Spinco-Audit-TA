import {
  ArrowLeft,
  BadgeCheck,
  ChevronLeft,
  ChevronRight,
  Download,
  Eye,
  Maximize,
  Minus,
  Plus,
  Printer,
  RefreshCw,
  ScanLine,
} from 'lucide-react'
import { useEffect, useRef, useState } from 'react'
import { Document, Page } from 'react-pdf'
import 'react-pdf/dist/Page/AnnotationLayer.css'
import 'react-pdf/dist/Page/TextLayer.css'
import { useNavigate, useParams } from 'react-router-dom'
import { apiFetch, apiPost } from '../api/client'
import { documentFileUrl, fetchDocument, fetchExtraction, reprocessDocument } from '../api/uploads'
import { useApi } from '../api/useApi'
import { useAuth } from '../auth/AuthContext'
import { Button } from '../components/ui/Button'
import { ErrorState } from '../components/ui/ErrorState'
import { LoadingState } from '../components/ui/LoadingState'
import { Modal } from '../components/ui/Modal'
import { StatusBadge } from '../components/ui/StatusBadge'
import { formatISTDateTime } from '../lib/datetime'

const VERIFY_ROLES = new Set(['ADMIN', 'AUDITOR'])

const MIN_SCALE = 0.5
const MAX_SCALE = 2.5

function formatBytes(bytes) {
  if (bytes === null || bytes === undefined) return '-'
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`
  return `${(bytes / (1024 * 1024)).toFixed(2)} MB`
}

function formatINR(amount) {
  if (amount === null || amount === undefined) return 'Pending extraction'
  return `Rs.${Number(amount).toLocaleString('en-IN', { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`
}

function ViewSourceButton({ page, onJump }) {
  if (!page) return null
  return (
    <button className="btn btn-secondary view-source-btn" onClick={() => onJump(page)}>
      <Eye size={13} /> View Source (Page {page})
    </button>
  )
}

function FieldRow({ label, value, page, onJump }) {
  return (
    <div className="viewer-meta-row">
      <span className="viewer-meta-label">{label}</span>
      <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', gap: 10 }}>
        <span className="viewer-meta-value">{value ?? 'Pending extraction'}</span>
        <ViewSourceButton page={page} onJump={onJump} />
      </div>
    </div>
  )
}

function ExtractedDetailsPanel({ extraction, onJump, onReprocess, reprocessing }) {
  const isExpense = extraction.source_type === 'EXPENSE_REPORT'
  const header = extraction.header
  const trip = extraction.trip

  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: 16 }}>
      <div className="card">
        <div className="card-header">
          <div>
            <div className="card-title">Extraction Result</div>
            <div className="card-subtitle">Fields extracted directly from the source document</div>
          </div>
          <div style={{ display: 'flex', alignItems: 'center', gap: 10 }}>
            <StatusBadge status={extraction.extraction_status} />
            <Button variant="secondary" icon={RefreshCw} onClick={onReprocess} disabled={reprocessing}>
              {reprocessing ? 'Reprocessing...' : 'Reprocess'}
            </Button>
          </div>
        </div>
        <div className="card-body">
          {extraction.extraction_status === 'REVIEW_REQUIRED' && (
            <div className="review-banner">
              <strong>Review Required.</strong> {extraction.extraction_notes || 'Some fields could not be reliably extracted.'}
            </div>
          )}
          {extraction.extraction_confidence && (
            <div style={{ fontSize: 12.5, color: 'var(--gray-500)', marginBottom: 12 }}>
              Extraction confidence: <strong>{extraction.extraction_confidence}</strong>
            </div>
          )}

          {isExpense ? (
            <>
              <FieldRow label="Expense Report ID" value={header.er_id} page={header.er_id_source_page} onJump={onJump} />
              <FieldRow label="Employee" value={header.employee_name} page={header.employee_name_source_page} onJump={onJump} />
              <FieldRow label="Employee ID" value={header.employee_code} page={header.employee_code_source_page} onJump={onJump} />
              <FieldRow label="Department" value={header.department} onJump={onJump} />
              <FieldRow label="Location" value={header.location} onJump={onJump} />
              <FieldRow label="Report Month" value={header.report_month_label} onJump={onJump} />
              <FieldRow label="Submission Date" value={header.submitted_on} page={header.submitted_on_source_page} onJump={onJump} />
              <FieldRow
                label="Total Claimed"
                value={formatINR(header.total_claimed)}
                page={header.total_claimed_source_page}
                onJump={onJump}
              />
              <FieldRow label="Expense Count" value={header.total_expenses} onJump={onJump} />
            </>
          ) : (
            <>
              <FieldRow label="Trip ID" value={trip.trip_id} page={trip.trip_id_source_page} onJump={onJump} />
              <FieldRow label="Employee" value={trip.employee_name} page={trip.employee_name_source_page} onJump={onJump} />
              <FieldRow label="Employee ID" value={trip.employee_code} onJump={onJump} />
              <FieldRow label="Travel Period" value={trip.start_date && trip.end_date ? `${trip.start_date} - ${trip.end_date}` : null} onJump={onJump} />
              <FieldRow label="Origin" value={trip.origin} onJump={onJump} />
              <FieldRow label="Destination" value={trip.destination} page={trip.destination_source_page} onJump={onJump} />
              <FieldRow label="Purpose" value={trip.purpose} page={trip.purpose_source_page} onJump={onJump} />
              <FieldRow label="Approval Status" value={trip.approval_status} onJump={onJump} />
              <FieldRow label="Approval Date" value={trip.approval_date} onJump={onJump} />
              <FieldRow label="Approver" value={trip.approver} page={trip.approver_source_page} onJump={onJump} />
            </>
          )}
        </div>
      </div>

      {isExpense && extraction.embedded_trip_summary && (
        <div className="card">
          <div className="card-header">
            <div>
              <div className="card-title">Embedded Trip Summary</div>
              <div className="card-subtitle">Source: this Expense Report PDF (not a standalone Trip Report)</div>
            </div>
            <StatusBadge status={extraction.embedded_trip_summary.extraction_status} />
          </div>
          <div className="card-body">
            <FieldRow
              label="Trip Number"
              value={extraction.embedded_trip_summary.trip_id}
              page={extraction.embedded_trip_summary.source_page}
              onJump={onJump}
            />
            <FieldRow label="City" value={extraction.embedded_trip_summary.destination} onJump={onJump} />
            <div style={{ marginTop: 14 }}>
              <div className="section-title" style={{ fontSize: 13 }}>
                Itinerary Legs
              </div>
              <div className="table-wrap">
                <table className="data-table">
                  <thead>
                    <tr>
                      <th>Leg</th>
                      <th>From</th>
                      <th>To</th>
                    </tr>
                  </thead>
                  <tbody>
                    {extraction.embedded_trip_summary.legs.map((leg) => (
                      <tr key={leg.leg_no}>
                        <td>{leg.leg_no}</td>
                        <td>
                          {leg.from_location} ({leg.from_date})
                        </td>
                        <td>
                          {leg.to_location} ({leg.to_date})
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            </div>
          </div>
        </div>
      )}
    </div>
  )
}

function ExpenseSummaryPanel({ extraction, onJump }) {
  const billsById = Object.fromEntries((extraction.bills || []).map((b) => [b.id, b]))

  return (
    <div className="card">
      <div className="card-header">
        <div className="card-title">Expense Summary</div>
        <div className="card-subtitle">{extraction.expenses.length} expense line(s) extracted from the source document</div>
      </div>
      <div className="card-body">
        <div className="table-wrap">
          <table className="data-table">
            <thead>
              <tr>
                <th>S.No</th>
                <th>Date</th>
                <th>Category</th>
                <th>Merchant</th>
                <th>Expense Details</th>
                <th className="num">Claimed Amount</th>
                <th className="num">Bill Amount</th>
                <th>Bill Date</th>
                <th>Match Status</th>
                <th>Trip Match</th>
                <th>Validation</th>
                <th>Source</th>
              </tr>
            </thead>
            <tbody>
              {extraction.expenses.map((e) => {
                const matchedBill = e.bill_id ? billsById[e.bill_id] : null
                const isBatta = (e.category || '').toUpperCase().replace(/\s*\[\d+\]\s*$/, '').trim() === 'BATTA'
                return (
                  <tr key={e.s_no}>
                    <td>{e.s_no}</td>
                    <td>{e.expense_date || '-'}</td>
                    <td>{e.category || '-'}</td>
                    <td>{e.merchant || '-'}</td>
                    <td>{e.expense_details || '-'}</td>
                    <td className="num">{formatINR(e.claimed_amount)}</td>
                    <td className="num">{e.bill_found ? formatINR(e.bill_amount) : '-'}</td>
                    <td>{e.bill_found ? e.bill_date || '-' : '-'}</td>
                    <td>
                      <StatusBadge status={e.bill_match_status} />
                    </td>
                    <td>{isBatta ? <StatusBadge status={e.trip_match_status} /> : '-'}</td>
                    <td>{e.validation_status ? <StatusBadge status={e.validation_status} /> : '-'}</td>
                    <td style={{ display: 'flex', gap: 6 }}>
                      <ViewSourceButton page={e.source_page} onJump={onJump} />
                      {matchedBill && <ViewSourceButton page={matchedBill.page_number} onJump={onJump} />}
                    </td>
                  </tr>
                )
              })}
            </tbody>
          </table>
        </div>
      </div>
    </div>
  )
}

function AttachedBillsPanel({ extraction, onJump }) {
  const bills = extraction.bills || []
  return (
    <div className="card">
      <div className="card-header">
        <div className="card-title">Attached Bills</div>
        <div className="card-subtitle">{bills.length} supporting document page(s) detected in this PDF</div>
      </div>
      <div className="card-body">
        {bills.length === 0 ? (
          <div className="state-panel" style={{ padding: '32px 16px' }}>
            <div className="state-title">No supporting bills detected</div>
            <div className="state-description">
              No pages after the expense table matched the evidence this system looks for (receipts, invoices, GSTIN,
              amounts with dates).
            </div>
          </div>
        ) : (
          <div className="table-wrap">
            <table className="data-table">
              <thead>
                <tr>
                  <th>Page</th>
                  <th>Vendor</th>
                  <th>Bill No.</th>
                  <th>Bill Date</th>
                  <th className="num">Amount</th>
                  <th>GSTIN</th>
                  <th>Extraction</th>
                  <th>Confidence</th>
                  <th>Match</th>
                  <th>Source</th>
                </tr>
              </thead>
              <tbody>
                {bills.map((b) => (
                  <tr key={b.id}>
                    <td>{b.page_number}</td>
                    <td>{b.vendor || '-'}</td>
                    <td>{b.bill_number || '-'}</td>
                    <td>{b.bill_date || '-'}</td>
                    <td className="num">{formatINR(b.amount)}</td>
                    <td>{b.gstin || '-'}</td>
                    <td>
                      <StatusBadge status={b.extraction_status} />
                    </td>
                    <td>{b.extraction_confidence || '-'}</td>
                    <td>
                      <StatusBadge status={b.match_status} />
                    </td>
                    <td>
                      <ViewSourceButton page={b.page_number} onJump={onJump} />
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </div>
    </div>
  )
}

function ReportStatusBanner({ extraction }) {
  const status = extraction.overall_status
  const total = extraction.total_expenses ?? 0
  const verified = extraction.verified_expense_count ?? 0
  const exceptions = extraction.exception_count ?? 0
  const review = extraction.review_required_count ?? 0

  return (
    <div className="card">
      <div className="card-header">
        <div>
          <div className="card-title">Report Status</div>
          <div className="card-subtitle">Deterministic aggregate of every expense's policy, bill and trip checks</div>
        </div>
        <div>{status ? <StatusBadge status={status} /> : <span style={{ color: 'var(--gray-500)' }}>Pending validation</span>}</div>
      </div>
      <div className="card-body">
        <div style={{ display: 'flex', gap: 24, flexWrap: 'wrap' }}>
          <div>
            <div style={{ fontSize: 22, fontWeight: 700 }}>{total}</div>
            <div style={{ fontSize: 12.5, color: 'var(--gray-500)' }}>Expenses</div>
          </div>
          <div>
            <div style={{ fontSize: 22, fontWeight: 700, color: 'var(--green-600, #16a34a)' }}>{verified}</div>
            <div style={{ fontSize: 12.5, color: 'var(--gray-500)' }}>Passed</div>
          </div>
          <div>
            <div style={{ fontSize: 22, fontWeight: 700, color: 'var(--red-600, #dc2626)' }}>{exceptions}</div>
            <div style={{ fontSize: 12.5, color: 'var(--gray-500)' }}>Exceptions</div>
          </div>
          <div>
            <div style={{ fontSize: 22, fontWeight: 700, color: 'var(--amber-600, #d97706)' }}>{review}</div>
            <div style={{ fontSize: 12.5, color: 'var(--gray-500)' }}>Review Required</div>
          </div>
        </div>
      </div>
    </div>
  )
}

function VerifyModal({ exception, onClose, onVerified }) {
  const isException = exception.severity === 'EXCEPTION'
  const [note, setNote] = useState('')
  const [error, setError] = useState(null)
  const [submitting, setSubmitting] = useState(false)

  async function handleConfirm() {
    setError(null)
    if (isException && !note.trim()) {
      setError('A note explaining why this exception is acceptable is required.')
      return
    }
    setSubmitting(true)
    try {
      await apiPost(`/exceptions/${exception.id}/verify`, { note: note.trim() || null })
      onVerified()
      onClose()
    } catch (err) {
      setError(err.message || 'Could not verify this condition.')
    } finally {
      setSubmitting(false)
    }
  }

  const label = isException
    ? 'Verify / Accept Exception'
    : exception.exception_type === 'TRIP MATCH REVIEW REQUIRED'
      ? 'Verify Trip Match'
      : exception.exception_type.startsWith('BILL MATCH')
        ? 'Verify Bill Match'
        : 'Verify'

  return (
    <Modal title={label} onClose={onClose}>
      <p style={{ fontSize: 13, color: 'var(--gray-700)', marginBottom: 4 }}>
        {isException
          ? 'This is a confirmed policy exception, not an ambiguous match. Accepting it overrides an automated finding - explain why below (e.g. a legitimate rounding or GST difference).'
          : 'Mark this review condition as verified?'}
      </p>
      <div style={{ display: 'flex', flexDirection: 'column', gap: 6, fontSize: 12.5, background: 'var(--gray-50)', borderRadius: 8, padding: 12, marginBottom: 12 }}>
        <div>
          <strong>Date:</strong> {exception.expense_date || '-'}
        </div>
        <div>
          <strong>Category:</strong> {exception.category || '-'}
        </div>
        <div>
          <strong>Policy Category:</strong> {exception.policy_category || 'Not Found'}
        </div>
        <div>
          <strong>Claimed:</strong> {exception.claimed_amount != null ? formatINR(exception.claimed_amount) : '-'}
        </div>
        <div>
          <strong>Bill / Limit:</strong>{' '}
          {exception.bill_amount != null ? formatINR(exception.bill_amount) : exception.policy_limit != null ? formatINR(exception.policy_limit) : '-'}
        </div>
        <div>
          <strong>Source:</strong> {exception.source_page != null ? `Page ${exception.source_page}` : 'Source not available'}
        </div>
        <div>
          <strong>Reason:</strong> {exception.reason}
        </div>
      </div>
      {error && <div className="form-error">{error}</div>}
      <div className="form-field">
        <label>{isException ? 'Note (required)' : 'Note (optional)'}</label>
        <textarea
          className="input"
          rows={3}
          value={note}
          onChange={(e) => setNote(e.target.value)}
          placeholder="Why this condition is acceptable, e.g. checked manually against the source PDF."
        />
      </div>
      <div className="form-actions">
        <button type="button" className="btn btn-secondary" onClick={onClose} disabled={submitting}>
          Cancel
        </button>
        <button type="button" className="btn btn-primary" onClick={handleConfirm} disabled={submitting}>
          {submitting ? 'Verifying...' : 'Confirm Verification'}
        </button>
      </div>
    </Modal>
  )
}

function ExceptionsTable({ rows, canVerify, onVerifyClick, onJump, showStatusAction }) {
  return (
    // table-wrap--scroll bounds the container's height so its horizontal
    // scrollbar stays at the bottom of the VISIBLE area (not below the last row,
    // which is off-screen once the Reason text wraps), and data-table--wide gives
    // the columns a readable minimum width instead of squeezing them.
    <div className="table-wrap table-wrap--scroll" tabIndex={0} role="region" aria-label="Exceptions table (scrollable)">
      <table className="data-table data-table--wide">
        <thead>
          <tr>
            <th>S.No</th>
            <th>Date</th>
            <th>Category</th>
            <th>Policy Category</th>
            <th>Type</th>
            <th>Severity</th>
            <th className="num">Claimed</th>
            <th className="num">Bill / Limit</th>
            <th>Reason</th>
            <th>Source</th>
            <th>{showStatusAction ? 'Status' : 'Verified'}</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((e) => (
            <tr key={e.id}>
              <td>{e.s_no ?? '-'}</td>
              <td>{e.expense_date || '-'}</td>
              <td>{e.category || '-'}</td>
              <td>{e.policy_category || 'Not Found'}</td>
              <td>{e.exception_type}</td>
              <td>
                <StatusBadge status={e.severity} />
              </td>
              <td className="num">{e.claimed_amount != null ? formatINR(e.claimed_amount) : '-'}</td>
              <td className="num">
                {e.bill_amount != null ? formatINR(e.bill_amount) : e.policy_limit != null ? formatINR(e.policy_limit) : '-'}
              </td>
              <td className="cell-reason">{e.reason}</td>
              <td className="cell-nowrap">
                {e.source_page != null ? (
                  <ViewSourceButton page={e.source_page} onJump={onJump} />
                ) : (
                  <span style={{ fontSize: 12, color: 'var(--gray-500)' }}>Source not available</span>
                )}
              </td>
              <td className="cell-nowrap">
                {e.status === 'RESOLVED' ? (
                  <span
                    style={{ display: 'flex', alignItems: 'center', gap: 5, fontSize: 12, color: 'var(--success, #16a34a)' }}
                    title={`Verified by ${e.verified_by_name || 'unknown'} on ${e.verified_at ? formatISTDateTime(e.verified_at) : ''}${e.verification_note ? ` - ${e.verification_note}` : ''}`}
                  >
                    <BadgeCheck size={14} /> {e.severity === 'EXCEPTION' ? 'Verified / Accepted' : 'Verified'}
                  </span>
                ) : showStatusAction && (e.severity === 'REVIEW REQUIRED' || e.severity === 'EXCEPTION') && canVerify ? (
                  <button className="btn btn-secondary" style={{ padding: '4px 10px', fontSize: 12 }} onClick={() => onVerifyClick(e)}>
                    {e.severity === 'EXCEPTION' ? 'Verify / Accept' : 'Verify'}
                  </button>
                ) : (
                  '-'
                )}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  )
}

const EXCEPTION_VIEW = 'EXCEPTION'
const REVIEW_VIEW = 'REVIEW REQUIRED'

function ExceptionsPanel({ extraction, documentId, onJump }) {
  const reportId = documentId.split(':')[1]
  const { data: exceptions, loading, error, refetch } = useApi(`/expense-reports/${reportId}/exceptions`)
  const { role } = useAuth()
  const [verifying, setVerifying] = useState(null)
  const canVerify = VERIFY_ROLES.has(role)

  // Verified/resolved items are never deleted (full audit history survives in
  // the backend - see ExceptionVerification) but they must not keep counting
  // as an ACTIVE, unresolved problem in the auditor's working view once
  // someone has actually verified them. Split here, not by hiding rows with
  // CSS - both lists render from the same real e.status the backend computed.
  const activeExceptions = (exceptions || []).filter((e) => e.status !== 'RESOLVED')
  const resolvedExceptions = (exceptions || []).filter((e) => e.status === 'RESOLVED')

  // Exceptions (definitive policy/evidence violations - AMOUNT MISMATCH,
  // DATE MISMATCH, CATEGORY MISMATCH, DUPLICATE BILL, OVER LIMIT, MISSING
  // BILL, etc.) and Review Required (uncertain cases needing human judgment
  // - ambiguous matching, insufficient OCR evidence, unresolved
  // classification) are kept in separate sections rather than one combined
  // table, so an auditor can immediately see how many of each exist and
  // triage them differently - a confirmed violation and "we need you to
  // look at this" are not the same kind of problem. Both read the SAME
  // underlying e.severity value the backend already computes (see
  // exception_engine.py's EXCEPTION/REVIEW_REQUIRED constants) - this is a
  // display split only, never a reclassification; nothing here changes
  // which severity a record has.
  const genuineExceptions = activeExceptions.filter((e) => e.severity === 'EXCEPTION')
  const reviewRequiredItems = activeExceptions.filter((e) => e.severity === 'REVIEW REQUIRED')

  // The section header counts intentionally use the report's own canonical
  // exception_count/review_required_count (also shown in ReportStatusBanner
  // just above, and on the Expense Reports list, Home dashboard, and
  // Analytics) rather than genuineExceptions.length/reviewRequiredItems.length
  // directly - those count DISTINCT EXPENSES whose validation_status landed
  // on EXCEPTION or REVIEW REQUIRED (exception_engine.py's report.exception_count/
  // review_required_count), while the row arrays below count individual
  // ExceptionRecord rows - one expense can genuinely fail two different
  // checks (e.g. both an amount mismatch and a date mismatch), producing two
  // real, separately-reportable rows for one counted expense. Showing every
  // row is correct (each is a distinct underlying problem, not a duplicate -
  // see module notes above), but the HEADER number must match the one
  // already shown elsewhere on this same page and everywhere else in the
  // app, or the counts would visibly disagree.
  const exceptionHeaderCount = extraction?.exception_count ?? genuineExceptions.length
  const reviewRequiredHeaderCount = extraction?.review_required_count ?? reviewRequiredItems.length

  // One selectable view at a time. The choice only decides which already-
  // classified rows are displayed - it never touches a record's severity.
  // Until the user picks, open on Exceptions, or on Review Required when the
  // report has none of the former (so nobody lands on an empty tab by default).
  const [selectedView, setSelectedView] = useState(null)
  const view = selectedView ?? (exceptionHeaderCount === 0 && reviewRequiredHeaderCount > 0 ? REVIEW_VIEW : EXCEPTION_VIEW)
  const isExceptionView = view === EXCEPTION_VIEW
  const visibleRows = isExceptionView ? genuineExceptions : reviewRequiredItems
  const visibleCount = isExceptionView ? exceptionHeaderCount : reviewRequiredHeaderCount

  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: 16 }}>
      <ReportStatusBanner extraction={extraction} />

      <div className="btn-group" role="tablist" aria-label="Exception views" style={{ alignSelf: 'flex-start' }}>
        <button
          type="button"
          role="tab"
          id="exceptions-tab-exception"
          aria-selected={isExceptionView}
          aria-controls="exceptions-tabpanel"
          className={isExceptionView ? 'active' : ''}
          onClick={() => setSelectedView(EXCEPTION_VIEW)}
        >
          Exceptions ({exceptionHeaderCount})
        </button>
        <button
          type="button"
          role="tab"
          id="exceptions-tab-review"
          aria-selected={!isExceptionView}
          aria-controls="exceptions-tabpanel"
          className={!isExceptionView ? 'active' : ''}
          onClick={() => setSelectedView(REVIEW_VIEW)}
        >
          Review Required ({reviewRequiredHeaderCount})
        </button>
      </div>

      <div
        className="card"
        id="exceptions-tabpanel"
        role="tabpanel"
        aria-labelledby={isExceptionView ? 'exceptions-tab-exception' : 'exceptions-tab-review'}
        data-view={isExceptionView ? 'exceptions' : 'review-required'}
      >
        <div className="card-header">
          <div>
            <div className="card-title">{isExceptionView ? 'Exceptions' : 'Review Required'} ({visibleCount})</div>
            <div className="card-subtitle">
              {isExceptionView
                ? 'Confirmed policy/evidence violations - amount, date, or category mismatches, duplicate bills, over-limit claims, and other definitive issues'
                : 'Uncertain cases needing human judgment - ambiguous bill matching, insufficient evidence, or unresolved classification'}
            </div>
          </div>
        </div>
        <div className="card-body">
          {loading && <LoadingState rows={3} />}
          {error && <ErrorState title="Could not load exceptions" description="The server may be unreachable." />}
          {!loading && !error && visibleRows.length === 0 && (
            <div className="state-panel" style={{ padding: '32px 16px' }}>
              <div className="state-title">{isExceptionView ? 'No exceptions' : 'Nothing needs review'}</div>
              <div className="state-description">
                {isExceptionView
                  ? 'No confirmed policy or evidence violations on this report.'
                  : 'Every applicable check currently has enough evidence to decide.'}
              </div>
            </div>
          )}
          {!loading && !error && visibleRows.length > 0 && (
            <>
              {visibleRows.length !== visibleCount && (
                <div className="card-subtitle" style={{ marginBottom: 10 }}>
                  {visibleRows.length} records across {visibleCount} {visibleCount === 1 ? 'expense' : 'expenses'} - one expense can fail more than one check.
                </div>
              )}
              <ExceptionsTable rows={visibleRows} canVerify={canVerify} onVerifyClick={setVerifying} onJump={onJump} showStatusAction />
            </>
          )}
        </div>
      </div>

      {!loading && !error && resolvedExceptions.length > 0 && (
        <details className="card">
          <summary className="card-header" style={{ cursor: 'pointer', listStyle: 'none' }}>
            <div>
              <div className="card-title">Verification History ({resolvedExceptions.length})</div>
              <div className="card-subtitle">Resolved - verified/accepted by an auditor. Kept for audit history, no longer counted as active.</div>
            </div>
          </summary>
          <div className="card-body">
            <ExceptionsTable rows={resolvedExceptions} canVerify={canVerify} onVerifyClick={setVerifying} onJump={onJump} />
          </div>
        </details>
      )}

      {verifying && (
        <VerifyModal
          exception={verifying}
          onClose={() => setVerifying(null)}
          onVerified={refetch}
        />
      )}
    </div>
  )
}

// The operational status an auditor sees day-to-day: once manually verified,
// the CURRENT state is "Matched / Verified" (for an accepted ambiguous-match
// review) or "Verified / Accepted" (everything else) - never a still-looking-
// unresolved EXCEPTION/REVIEW REQUIRED badge. The original automated result
// is never discarded - see the tooltip built alongside this in each caller.
function operationalStatusLabel(v) {
  if (!v.manually_verified) return null
  return (v.resolved_exception_type || '').includes('MATCH') ? 'MATCHED' : 'VERIFIED'
}

function ValidationResultCell({ v }) {
  const opStatus = operationalStatusLabel(v)
  if (!opStatus) return <StatusBadge status={v.result} />
  return (
    <div style={{ display: 'flex', alignItems: 'center', gap: 6 }}>
      <StatusBadge status={opStatus} />
      <span
        style={{ fontSize: 11, color: 'var(--gray-500)' }}
        title={`Manually verified/accepted by ${v.verified_by_name || 'unknown'}${v.verified_at ? ` on ${formatISTDateTime(v.verified_at)}` : ''} - preserved for audit history`}
      >
        (was {v.result === 'REVIEW_REQUIRED' ? 'Review Required' : 'Exception'})
      </span>
    </div>
  )
}

function ValidationResultsPanel({ extraction, onJump }) {
  const validations = extraction.validations || []
  const expensesBySNo = Object.fromEntries((extraction.expenses || []).map((e) => [e.id, e.s_no]))
  const reportLevel = validations.filter((v) => v.expense_id === null)
  const perExpense = validations.filter((v) => v.expense_id !== null)

  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: 16 }}>
      <div className="card">
        <div className="card-header">
          <div>
            <div className="card-title">Validation Results</div>
            <div className="card-subtitle">
              {validations.length} deterministic policy check(s) run against Policy Master and the matched supporting bills
            </div>
          </div>
        </div>
        <div className="card-body">
          {validations.length === 0 ? (
            <div className="state-panel" style={{ padding: '32px 16px' }}>
              <div className="state-title">No validation results yet</div>
              <div className="state-description">Validation runs automatically after extraction and bill matching complete.</div>
            </div>
          ) : (
            <div className="table-wrap">
              <table className="data-table">
                <thead>
                  <tr>
                    <th>S.No</th>
                    <th>Check Type</th>
                    <th>Policy Category</th>
                    <th>Details</th>
                    <th>Result</th>
                    <th>Remarks</th>
                    <th>Source</th>
                  </tr>
                </thead>
                <tbody>
                  {reportLevel.map((v) => (
                    <tr key={v.id}>
                      <td>-</td>
                      <td>{v.check_type}</td>
                      <td>{v.policy_category || 'Not Found'}</td>
                      <td>{v.details || '-'}</td>
                      <td>
                        <ValidationResultCell v={v} />
                      </td>
                      <td>{v.remarks || '-'}</td>
                      <td>
                        <ViewSourceButton page={v.source_page} onJump={onJump} />
                      </td>
                    </tr>
                  ))}
                  {perExpense.map((v) => (
                    <tr key={v.id}>
                      <td>{expensesBySNo[v.expense_id] ?? '-'}</td>
                      <td>{v.check_type}</td>
                      <td>{v.policy_category || 'Not Found'}</td>
                      <td>{v.details || '-'}</td>
                      <td>
                        <ValidationResultCell v={v} />
                      </td>
                      <td>{v.remarks || '-'}</td>
                      <td>
                        <ViewSourceButton page={v.source_page} onJump={onJump} />
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </div>
      </div>
    </div>
  )
}

function LinkedBattaClaimsPanel({ documentId }) {
  const reportId = documentId.split(':')[1]
  const { data: matches, loading, error } = useApi(`/trip-reports/${reportId}/batta-matches`)

  return (
    <div className="card">
      <div className="card-header">
        <div>
          <div className="card-title">Linked Batta Claims</div>
          <div className="card-subtitle">BATTA expenses that were matched to, or considered against, this trip</div>
        </div>
      </div>
      <div className="card-body">
        {loading && <LoadingState rows={3} />}
        {error && <ErrorState title="Could not load Batta matches" description="The server may be unreachable." />}
        {!loading && !error && (!matches || matches.length === 0) && (
          <div className="state-panel" style={{ padding: '32px 16px' }}>
            <div className="state-title">No Batta claims linked yet</div>
            <div className="state-description">No BATTA expense has been matched to, or considered against, this trip so far.</div>
          </div>
        )}
        {!loading && !error && matches && matches.length > 0 && (
          <div className="table-wrap">
            <table className="data-table">
              <thead>
                <tr>
                  <th>Report</th>
                  <th>S.No</th>
                  <th>Date</th>
                  <th className="num">Claimed Amount</th>
                  <th>Status</th>
                  <th>Score</th>
                  <th>Evidence</th>
                </tr>
              </thead>
              <tbody>
                {matches.map((m) => (
                  <tr key={m.expense_id}>
                    <td>
                      <a href={`/documents/expense:${m.expense_report_id}`}>Expense Report #{m.expense_report_id}</a>
                    </td>
                    <td>{m.s_no ?? '-'}</td>
                    <td>{m.expense_date || '-'}</td>
                    <td className="num">{formatINR(m.claimed_amount)}</td>
                    <td>
                      <StatusBadge status={m.is_matched_here ? 'MATCHED' : m.trip_match_status} />
                    </td>
                    <td>{m.score ?? '-'}</td>
                    <td style={{ fontSize: 12.5, color: 'var(--gray-500)' }}>{m.reasons || '-'}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </div>
    </div>
  )
}

function TripLegsPanel({ extraction }) {
  return (
    <div className="card">
      <div className="card-header">
        <div className="card-title">Itinerary</div>
        <div className="card-subtitle">{extraction.legs.length} leg(s) extracted from the source document</div>
      </div>
      <div className="card-body">
        <div className="table-wrap">
          <table className="data-table">
            <thead>
              <tr>
                <th>Leg</th>
                <th>From</th>
                <th>From Date</th>
                <th>To</th>
                <th>To Date</th>
                <th>Description</th>
              </tr>
            </thead>
            <tbody>
              {extraction.legs.map((leg) => (
                <tr key={leg.leg_no}>
                  <td>{leg.leg_no}</td>
                  <td>{leg.from_location || '-'}</td>
                  <td>{leg.from_date || '-'}</td>
                  <td>{leg.to_location || '-'}</td>
                  <td>{leg.to_date || '-'}</td>
                  <td>{leg.description || '-'}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </div>
    </div>
  )
}

export default function DocumentViewerPage() {
  const { documentId } = useParams()
  const navigate = useNavigate()

  const [doc, setDoc] = useState(null)
  const [extraction, setExtraction] = useState(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState(null)
  const [reprocessing, setReprocessing] = useState(false)

  const [tab, setTab] = useState('pdf')
  const [fileData, setFileData] = useState(null)
  const [fileLoadError, setFileLoadError] = useState(false)
  const [numPages, setNumPages] = useState(null)
  const [pageNumber, setPageNumber] = useState(1)
  const [pageInput, setPageInput] = useState('1')
  const [scale, setScale] = useState(1.1)
  const [pageWidth, setPageWidth] = useState(null)

  const canvasWrapRef = useRef(null)
  const shellRef = useRef(null)

  function loadDocument(cancelledRef) {
    setLoading(true)
    setError(null)
    Promise.all([fetchDocument(documentId), fetchExtraction(documentId)])
      .then(([d, e]) => {
        if (cancelledRef?.current) return
        setDoc(d)
        setExtraction(e)
      })
      .catch((err) => {
        if (!cancelledRef?.current) setError(err)
      })
      .finally(() => {
        if (!cancelledRef?.current) setLoading(false)
      })

    // Fetched ourselves (rather than handed a bare URL to react-pdf/pdf.js) so a missing
    // or unreadable file surfaces as a normal, catchable error instead of an uncaught
    // exception from inside pdf.js's own fetch/worker pipeline.
    apiFetch(`/documents/${encodeURIComponent(documentId)}/file`)
      .then((res) => res.arrayBuffer())
      .then((buf) => {
        if (!cancelledRef?.current) setFileData(buf)
      })
      .catch(() => {
        if (!cancelledRef?.current) setFileLoadError(true)
      })
  }

  useEffect(() => {
    const cancelledRef = { current: false }
    // eslint-disable-next-line react-hooks/set-state-in-effect -- resetting loading/error for a new document id is intentional here
    setFileLoadError(false)
    setFileData(null)
    loadDocument(cancelledRef)
    return () => {
      cancelledRef.current = true
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [documentId])

  async function handleReprocess() {
    setReprocessing(true)
    try {
      await reprocessDocument(documentId)
      loadDocument()
    } catch {
      // extraction_status will already show FAILED once reloaded; nothing further to do here
    } finally {
      setReprocessing(false)
    }
  }

  function goToPage(page) {
    if (!page || Number.isNaN(page)) return
    const clamped = Math.max(1, Math.min(numPages || page, page))
    setPageNumber(clamped)
    setPageInput(String(clamped))
  }

  function jumpToPage(page) {
    goToPage(page)
    setTab('pdf')
  }

  // The typed page-number box is kept in sync with pageNumber by goToPage(),
  // which every page change (prev/next buttons, a thumbnail click, a Source
  // link jump from another tab, keyboard navigation) goes through - only the
  // box itself (via its own onChange) should ever diverge from pageNumber,
  // and only while the user is actively typing.

  // Keyboard navigation (Part 27): only while the PDF tab is active and
  // focus isn't in a text input/textarea (so this never hijacks typing in
  // the page-number box or anywhere else on the page).
  useEffect(() => {
    if (tab !== 'pdf') return undefined
    function onKeyDown(e) {
      const tagName = document.activeElement?.tagName
      if (tagName === 'INPUT' || tagName === 'TEXTAREA') return
      if (e.key === 'ArrowLeft' || e.key === 'PageUp') {
        e.preventDefault()
        goToPage(pageNumber - 1)
      } else if (e.key === 'ArrowRight' || e.key === 'PageDown') {
        e.preventDefault()
        goToPage(pageNumber + 1)
      } else if (e.key === 'Home') {
        e.preventDefault()
        goToPage(1)
      } else if (e.key === 'End' && numPages) {
        e.preventDefault()
        goToPage(numPages)
      }
    }
    window.addEventListener('keydown', onKeyDown)
    return () => window.removeEventListener('keydown', onKeyDown)
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [tab, pageNumber, numPages])

  function handleFit() {
    if (canvasWrapRef.current && pageWidth) {
      const available = canvasWrapRef.current.clientWidth - 48
      setScale(Math.max(MIN_SCALE, Math.min(MAX_SCALE, available / pageWidth)))
    }
  }

  function toggleFullscreen() {
    if (!document.fullscreenElement) {
      shellRef.current?.requestFullscreen?.()
    } else {
      document.exitFullscreen?.()
    }
  }

  if (loading) return <LoadingState rows={6} />
  if (error) return <ErrorState title="Could not load this document" description="It may have been deleted, or the server is unreachable." />
  if (!doc || !extraction) return null

  const fileUrl = documentFileUrl(documentId)
  const isExpense = doc.source_type === 'EXPENSE_REPORT'

  const TABS = [
    { key: 'pdf', label: 'PDF Document' },
    { key: 'extracted', label: 'Extracted Details' },
    { key: isExpense ? 'expenses' : 'trip', label: isExpense ? 'Expense Summary' : 'Trip Details' },
    ...(isExpense ? [{ key: 'exceptions', label: 'Exceptions' }] : []),
    ...(isExpense ? [{ key: 'bills', label: 'Attached Bills' }] : []),
    ...(isExpense ? [{ key: 'validations', label: 'Validation Results' }] : []),
    ...(!isExpense ? [{ key: 'batta', label: 'Linked Batta Claims' }] : []),
  ]

  return (
    <div className="viewer-shell" ref={shellRef}>
      <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between' }}>
        <button className="btn btn-secondary viewer-back" onClick={() => navigate(-1)}>
          <ArrowLeft size={15} /> Back
        </button>
        <div className="btn-group">
          {TABS.map((t) => (
            <button key={t.key} className={tab === t.key ? 'active' : ''} onClick={() => setTab(t.key)}>
              {t.label}
            </button>
          ))}
        </div>
      </div>

      {tab === 'pdf' && (
        <>
          <div className="viewer-toolbar">
            <div className="viewer-toolbar-group">
              <button
                className="icon-action viewer-nav-btn"
                title="Previous page (Left Arrow)"
                disabled={pageNumber <= 1}
                onClick={() => goToPage(pageNumber - 1)}
              >
                <ChevronLeft size={20} />
              </button>
              <span className="viewer-page-indicator">
                Page{' '}
                <input
                  className="viewer-page-input"
                  type="text"
                  inputMode="numeric"
                  value={pageInput}
                  aria-label="Go to page"
                  onChange={(e) => setPageInput(e.target.value.replace(/[^\d]/g, ''))}
                  onKeyDown={(e) => {
                    if (e.key === 'Enter') goToPage(parseInt(pageInput, 10))
                  }}
                  onBlur={() => goToPage(parseInt(pageInput, 10) || pageNumber)}
                />{' '}
                / {numPages || '-'}
              </span>
              <button
                className="icon-action viewer-nav-btn"
                title="Next page (Right Arrow)"
                disabled={!numPages || pageNumber >= numPages}
                onClick={() => goToPage(pageNumber + 1)}
              >
                <ChevronRight size={20} />
              </button>
            </div>

            <div className="viewer-toolbar-group">
              <button className="icon-action" onClick={() => setScale((s) => Math.max(MIN_SCALE, s - 0.15))}>
                <Minus size={15} />
              </button>
              <span className="viewer-page-indicator">{Math.round(scale * 100)}%</span>
              <button className="icon-action" onClick={() => setScale((s) => Math.min(MAX_SCALE, s + 0.15))}>
                <Plus size={15} />
              </button>
              <button className="icon-action" title="Fit to width" onClick={handleFit}>
                <ScanLine size={15} />
              </button>
              <button className="icon-action" title="Full screen" onClick={toggleFullscreen}>
                <Maximize size={15} />
              </button>
            </div>

            <div className="viewer-toolbar-group">
              <button className="icon-action" title="Print" onClick={() => window.open(fileUrl, '_blank')}>
                <Printer size={15} />
              </button>
              <a className="icon-action" title="Download" href={documentFileUrl(documentId, { download: true })}>
                <Download size={15} />
              </a>
            </div>
          </div>

          <div className="viewer-body">
            {fileLoadError ? (
              <div className="card" style={{ gridColumn: '1 / span 2' }}>
                <ErrorState
                  title="This document's PDF file is unavailable"
                  description="The stored file could not be found or read. Try Reprocess from Extracted Details, or contact an administrator."
                />
              </div>
            ) : !fileData ? (
              <div className="card" style={{ gridColumn: '1 / span 2' }}>
                <LoadingState rows={4} />
              </div>
            ) : (
              <Document
                file={{ data: fileData }}
                onLoadSuccess={({ numPages: n }) => setNumPages(n)}
                onLoadError={() => setFileLoadError(true)}
                loading={null}
                className="viewer-document-passthrough"
              >
                <div className="viewer-thumbnails">
                  {numPages &&
                    Array.from({ length: numPages }, (_, i) => i + 1).map((n) => (
                      <div key={n} className={`viewer-thumb${n === pageNumber ? ' active' : ''}`} onClick={() => goToPage(n)}>
                        <Page pageNumber={n} width={110} renderTextLayer={false} renderAnnotationLayer={false} />
                        <span className="viewer-thumb-number">{n}</span>
                      </div>
                    ))}
                </div>

                <div className="viewer-canvas-wrap" ref={canvasWrapRef}>
                  <Page pageNumber={pageNumber} scale={scale} onLoadSuccess={(page) => setPageWidth(page.originalWidth)} />
                </div>
              </Document>
            )}

            <div className="viewer-meta-panel">
              <div className="viewer-meta-row">
                <span className="viewer-meta-label">Filename</span>
                <span className="viewer-meta-value">{doc.original_filename}</span>
              </div>
              <div className="viewer-meta-row">
                <span className="viewer-meta-label">Document Type</span>
                <span className="viewer-meta-value">{isExpense ? 'Expense Report' : 'Trip Report'}</span>
              </div>
              <div className="viewer-meta-row">
                <span className="viewer-meta-label">Report / Trip ID</span>
                <span className="viewer-meta-value">{doc.document_number || 'Pending extraction'}</span>
              </div>
              <div className="viewer-meta-row">
                <span className="viewer-meta-label">Employee</span>
                <span className="viewer-meta-value">{doc.person_name || 'Pending extraction'}</span>
              </div>
              <div className="viewer-meta-row">
                <span className="viewer-meta-label">File Size</span>
                <span className="viewer-meta-value">{formatBytes(doc.file_size)}</span>
              </div>
              <div className="viewer-meta-row">
                <span className="viewer-meta-label">Pages</span>
                <span className="viewer-meta-value">{doc.page_count ?? '-'}</span>
              </div>
              <div className="viewer-meta-row">
                <span className="viewer-meta-label">Upload Status</span>
                <span className="viewer-meta-value">
                  <StatusBadge status={doc.processing_status} />
                </span>
              </div>
              <div className="viewer-meta-row">
                <span className="viewer-meta-label">Extraction Status</span>
                <span className="viewer-meta-value">
                  <StatusBadge status={doc.extraction_status} />
                </span>
              </div>
              <div className="viewer-meta-row">
                <span className="viewer-meta-label">Uploaded</span>
                <span className="viewer-meta-value">{formatISTDateTime(doc.uploaded_at)}</span>
              </div>
              <div className="viewer-meta-row">
                <span className="viewer-meta-label">File Hash (SHA-256)</span>
                <span className="viewer-meta-value" style={{ fontSize: 11, fontWeight: 500, fontFamily: 'monospace' }}>
                  {doc.file_hash}
                </span>
              </div>
            </div>
          </div>
        </>
      )}

      {tab === 'extracted' && (
        <div style={{ overflowY: 'auto' }}>
          <ExtractedDetailsPanel extraction={extraction} onJump={jumpToPage} onReprocess={handleReprocess} reprocessing={reprocessing} />
        </div>
      )}

      {tab === 'expenses' && (
        <div style={{ overflowY: 'auto' }}>
          <ExpenseSummaryPanel extraction={extraction} onJump={jumpToPage} />
        </div>
      )}

      {tab === 'trip' && (
        <div style={{ overflowY: 'auto' }}>
          <TripLegsPanel extraction={extraction} />
        </div>
      )}

      {tab === 'exceptions' && (
        <div style={{ overflowY: 'auto' }}>
          <ExceptionsPanel extraction={extraction} documentId={documentId} onJump={jumpToPage} />
        </div>
      )}

      {tab === 'bills' && (
        <div style={{ overflowY: 'auto' }}>
          <AttachedBillsPanel extraction={extraction} onJump={jumpToPage} />
        </div>
      )}

      {tab === 'validations' && (
        <div style={{ overflowY: 'auto' }}>
          <ValidationResultsPanel extraction={extraction} onJump={jumpToPage} />
        </div>
      )}

      {tab === 'batta' && (
        <div style={{ overflowY: 'auto' }}>
          <LinkedBattaClaimsPanel documentId={documentId} />
        </div>
      )}
    </div>
  )
}
