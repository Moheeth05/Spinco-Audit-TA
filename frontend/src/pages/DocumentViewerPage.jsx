import {
  ArrowLeft,
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
import { documentFileUrl, fetchDocument, fetchExtraction, reprocessDocument } from '../api/uploads'
import { Button } from '../components/ui/Button'
import { ErrorState } from '../components/ui/ErrorState'
import { LoadingState } from '../components/ui/LoadingState'
import { StatusBadge } from '../components/ui/StatusBadge'

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
                <th>Expense Details</th>
                <th className="num">Claimed Amount</th>
                <th>Status</th>
                <th>Source</th>
              </tr>
            </thead>
            <tbody>
              {extraction.expenses.map((e) => (
                <tr key={e.s_no}>
                  <td>{e.s_no}</td>
                  <td>{e.expense_date || '-'}</td>
                  <td>{e.category || '-'}</td>
                  <td>{e.expense_details || '-'}</td>
                  <td className="num">{formatINR(e.claimed_amount)}</td>
                  <td>
                    <StatusBadge status={e.extraction_status} />
                  </td>
                  <td>
                    <ViewSourceButton page={e.source_page} onJump={onJump} />
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
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
    fetch(documentFileUrl(documentId))
      .then((res) => {
        if (!res.ok) throw new Error('file unavailable')
        return res.arrayBuffer()
      })
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

  function jumpToPage(page) {
    setPageNumber(page)
    setTab('pdf')
  }

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
              <button className="icon-action" disabled={pageNumber <= 1} onClick={() => setPageNumber((p) => p - 1)}>
                <ChevronLeft size={15} />
              </button>
              <span className="viewer-page-indicator">
                Page {pageNumber} / {numPages || '-'}
              </span>
              <button
                className="icon-action"
                disabled={!numPages || pageNumber >= numPages}
                onClick={() => setPageNumber((p) => p + 1)}
              >
                <ChevronRight size={15} />
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
                      <div key={n} className={`viewer-thumb${n === pageNumber ? ' active' : ''}`} onClick={() => setPageNumber(n)}>
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
                <span className="viewer-meta-value">{new Date(doc.uploaded_at).toLocaleString()}</span>
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
    </div>
  )
}
