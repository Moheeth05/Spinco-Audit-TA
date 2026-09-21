import {
  ArrowLeft,
  ChevronLeft,
  ChevronRight,
  Download,
  Maximize,
  Minus,
  Plus,
  Printer,
  ScanLine,
} from 'lucide-react'
import { useEffect, useRef, useState } from 'react'
import { Document, Page } from 'react-pdf'
import 'react-pdf/dist/Page/AnnotationLayer.css'
import 'react-pdf/dist/Page/TextLayer.css'
import { useNavigate, useParams } from 'react-router-dom'
import { documentFileUrl, fetchDocument } from '../api/uploads'
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

export default function DocumentViewerPage() {
  const { documentId } = useParams()
  const navigate = useNavigate()

  const [doc, setDoc] = useState(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState(null)

  const [numPages, setNumPages] = useState(null)
  const [pageNumber, setPageNumber] = useState(1)
  const [scale, setScale] = useState(1.1)
  const [pageWidth, setPageWidth] = useState(null)

  const canvasWrapRef = useRef(null)
  const shellRef = useRef(null)

  useEffect(() => {
    let cancelled = false
    // eslint-disable-next-line react-hooks/set-state-in-effect -- resetting loading/error for a new request is intentional here
    setLoading(true)
    setError(null)
    fetchDocument(documentId)
      .then((d) => {
        if (!cancelled) setDoc(d)
      })
      .catch((err) => {
        if (!cancelled) setError(err)
      })
      .finally(() => {
        if (!cancelled) setLoading(false)
      })
    return () => {
      cancelled = true
    }
  }, [documentId])

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
  if (!doc) return null

  const fileUrl = documentFileUrl(documentId)

  return (
    <div className="viewer-shell" ref={shellRef}>
      <button className="btn btn-secondary viewer-back" onClick={() => navigate(-1)}>
        <ArrowLeft size={15} /> Back
      </button>

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
        <Document
          file={fileUrl}
          onLoadSuccess={({ numPages: n }) => setNumPages(n)}
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
            <Page
              pageNumber={pageNumber}
              scale={scale}
              onLoadSuccess={(page) => setPageWidth(page.originalWidth)}
            />
          </div>
        </Document>

        <div className="viewer-meta-panel">
          <div className="viewer-meta-row">
            <span className="viewer-meta-label">Filename</span>
            <span className="viewer-meta-value">{doc.original_filename}</span>
          </div>
          <div className="viewer-meta-row">
            <span className="viewer-meta-label">Document Type</span>
            <span className="viewer-meta-value">{doc.source_type === 'EXPENSE_REPORT' ? 'Expense Report' : 'Trip Report'}</span>
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
    </div>
  )
}
