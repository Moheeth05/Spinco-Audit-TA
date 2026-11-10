import { AlertTriangle, CheckCircle2, Clock, Eye, FileText, Loader2, Map as MapIcon, Trash2 } from 'lucide-react'
import { useEffect, useState } from 'react'
import { Link, useNavigate } from 'react-router-dom'
import { useCentralMode } from '../api/central'
import { useApi } from '../api/useApi'
import { deleteDocument, uploadFiles } from '../api/uploads'
import { Card } from '../components/ui/Card'
import { ProcessingStateBadge, StorageStateBadge } from '../components/ui/CentralBadges'
import { DataTable } from '../components/ui/DataTable'
import { Dropzone } from '../components/ui/Dropzone'
import { EmptyState } from '../components/ui/EmptyState'
import { ErrorState } from '../components/ui/ErrorState'
import { LoadingState } from '../components/ui/LoadingState'
import { ProgressBar } from '../components/ui/ProgressBar'
import { StatusBadge } from '../components/ui/StatusBadge'
import { formatISTLabel } from '../lib/datetime'

const TABS = [
  { key: 'EXPENSE_REPORT', label: 'Expense Reports', icon: FileText },
  { key: 'TRIP_REPORT', label: 'Trip Reports', icon: MapIcon },
]

function formatBytes(bytes) {
  if (bytes === null || bytes === undefined) return '-'
  if (bytes < 1024) return `${bytes} B`
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`
  return `${(bytes / (1024 * 1024)).toFixed(2)} MB`
}

// extraction_status values that mean the background extraction hasn't finished yet.
const EXTRACTION_PENDING = new Set(['NOT_PROCESSED', 'PROCESSING'])
const POLL_INTERVAL_MS = 3000

function isExtractionPending(upload) {
  return upload.processing_status !== 'FAILED' && EXTRACTION_PENDING.has(upload.extraction_status)
}

function QueueStatusIcon({ status }) {
  if (status === 'PROCESSING') return <Loader2 size={15} className="spin" color="var(--blue-600)" />
  if (status === 'PROCESSED') return <CheckCircle2 size={15} color="var(--success)" />
  if (status === 'FAILED') return <AlertTriangle size={15} color="var(--error)" />
  // Not stored, but not an error either: a duplicate or a temporary outage. Never the success icon.
  if (status === 'DUPLICATE' || status === 'DEFERRED') return <AlertTriangle size={15} color="var(--warning)" />
  return <Clock size={15} color="var(--gray-400)" />
}

/** Maps one upload response result onto the queue row's status/message.
 * `result.result` is the central-storage outcome; an older backend omits it,
 * in which case `success` decides. Only SUCCESS (with success true) is shown as processed. */
function outcomeOf(result) {
  const outcome = result.result || (result.success ? 'SUCCESS' : 'FAILED')
  if (outcome === 'SUCCESS' && result.success !== false) return { status: 'PROCESSED', errorMessage: result.error_message }
  if (outcome === 'DUPLICATE') return { status: 'DUPLICATE', errorMessage: result.error_message, duplicateOf: result.duplicate_of || {} }
  if (outcome === 'DEFERRED') {
    return { status: 'DEFERRED', errorMessage: 'Central storage is temporarily unavailable - nothing was saved; please retry' }
  }
  return { status: 'FAILED', errorMessage: result.error_message || 'Upload failed.' }
}

function QueueNote({ status, errorMessage, duplicateOf }) {
  if (status === 'DUPLICATE') {
    const name = duplicateOf?.original_filename
    return (
      <div className="queue-note">
        {name ? `Duplicate of ${name} - not stored again` : errorMessage || 'Duplicate of an existing file - not stored again'}
        {duplicateOf?.document_id && (
          <>
            {' '}
            <Link to={`/documents/${duplicateOf.document_id}`}>Open original</Link>
          </>
        )}
      </div>
    )
  }
  if (!errorMessage) return null
  return <div className={status === 'PROCESSING' ? 'queue-note' : 'queue-error'}>{errorMessage}</div>
}

export default function UploadPage() {
  const navigate = useNavigate()
  const centralMode = useCentralMode()
  const central = centralMode !== 'off'
  const [tab, setTab] = useState('EXPENSE_REPORT')
  const [queue, setQueue] = useState([]) // [{ filename, size, status, progress, errorMessage }]
  const { data: uploads, loading, error, refetch, refetchSilently } = useApi('/uploads')

  // The upload request returns as soon as the file is stored; extraction/OCR
  // continues in the background and can take minutes for a large scanned
  // report. Poll quietly until nothing is pending so the queue and the
  // Uploaded Files table reflect the real state without a manual refresh.
  const anyPending = (uploads || []).some(isExtractionPending)
  useEffect(() => {
    if (!anyPending) return undefined
    const timer = setInterval(refetchSilently, POLL_INTERVAL_MS)
    return () => clearInterval(timer)
  }, [anyPending, refetchSilently])

  const uploadsById = new Map((uploads || []).map((u) => [u.document_id, u]))

  // A queue row only counts as finished once extraction itself is done.
  function effectiveQueueState(row) {
    if (row.status !== 'PROCESSED') return { status: row.status, errorMessage: row.errorMessage }
    const upload = row.documentId ? uploadsById.get(row.documentId) : null
    if (!upload) return { status: row.status, errorMessage: row.errorMessage }
    if (isExtractionPending(upload)) return { status: 'PROCESSING', errorMessage: 'Extracting data from the PDF - this can take a few minutes for scanned reports.' }
    if (upload.extraction_status === 'FAILED') return { status: 'FAILED', errorMessage: 'Extraction failed. Open the file to see details or reprocess it.' }
    return { status: row.status, errorMessage: row.errorMessage }
  }

  async function handleFiles(files) {
    const queueEntries = files.map((f) => ({ filename: f.name, size: f.size, status: 'PROCESSING', progress: 0 }))
    setQueue((prev) => [...queueEntries, ...prev])

    try {
      const response = await uploadFiles(tab, files, (percent) => {
        setQueue((prev) =>
          prev.map((row) => (queueEntries.some((q) => q.filename === row.filename) ? { ...row, progress: percent } : row))
        )
      })

      setQueue((prev) =>
        prev.map((row) => {
          const result = response.results.find((r) => r.filename === row.filename)
          if (!result || !queueEntries.some((q) => q.filename === row.filename)) return row
          const outcome = outcomeOf(result)
          return {
            ...row,
            ...outcome,
            progress: 100,
            // a DUPLICATE/DEFERRED result created no document, so there is nothing to poll for
            documentId: outcome.status === 'PROCESSED' ? result.document_id : null,
          }
        })
      )
    } catch {
      setQueue((prev) =>
        prev.map((row) =>
          queueEntries.some((q) => q.filename === row.filename)
            ? { ...row, status: 'FAILED', errorMessage: 'Could not reach the server.' }
            : row
        )
      )
    } finally {
      refetch()
    }
  }

  async function handleDelete(documentId) {
    try {
      await deleteDocument(documentId)
      refetch()
    } catch {
      // surfaced inline below via a lightweight failure marker
    }
  }

  const filteredUploads = (uploads || []).filter((u) => u.source_type === tab)

  const columns = [
    { key: 'filename', header: 'File Name', render: (r) => `${r.original_filename} - ${r.person_name || 'Pending extraction'}` },
    { key: 'employee_code', header: 'Employee ID', render: (r) => r.employee_code || 'Pending extraction' },
    { key: 'document_number', header: 'Report / Trip ID', render: (r) => r.document_number || 'Pending extraction' },
    { key: 'file_size', header: 'File Size', align: 'right', render: (r) => formatBytes(r.file_size) },
    { key: 'uploaded_at', header: 'Upload Date', render: (r) => (r.uploaded_at ? formatISTLabel(r.uploaded_at) : r.uploaded_at_ist || '-') },
    ...(central
      ? [
          { key: 'uploaded_by_name', header: 'Uploaded By', render: (r) => r.uploaded_by_name || '-' },
          { key: 'document_type', header: 'Type', render: (r) => (r.source_type === 'TRIP_REPORT' ? 'Trip Report' : 'Expense Report') },
          { key: 'storage_state', header: 'Storage', render: (r) => <StorageStateBadge state={r.storage_state} /> },
          { key: 'processing_state', header: 'Central Processing', render: (r) => <ProcessingStateBadge state={r.processing_state} /> },
          {
            key: 'duplicate_count',
            header: 'Duplicates',
            align: 'right',
            render: (r) => (r.duplicate_count > 0 ? r.duplicate_count : '-'),
          },
        ]
      : []),
    {
      key: 'processing_status',
      header: 'Status',
      render: (r) => <StatusBadge status={isExtractionPending(r) ? 'PROCESSING' : r.processing_status} />,
    },
    {
      key: 'action',
      header: 'Action',
      render: (r) => (
        <div style={{ display: 'flex', gap: 8 }}>
          <button className="icon-action" title="View" onClick={() => navigate(`/documents/${r.document_id}`)}>
            <Eye size={14} />
          </button>
          <button className="icon-action is-danger" title="Delete" onClick={() => handleDelete(r.document_id)}>
            <Trash2 size={14} />
          </button>
        </div>
      ),
    },
  ]

  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: 20 }}>
      <div className="btn-group">
        {TABS.map((t) => (
          <button key={t.key} className={tab === t.key ? 'active' : ''} onClick={() => setTab(t.key)}>
            {t.label}
          </button>
        ))}
      </div>

      <Card
        title={tab === 'EXPENSE_REPORT' ? 'Upload Expense Reports' : 'Upload Trip Reports'}
        subtitle="Supports single-file and bulk/multiple-file upload. Files are organized by their own report date once processed, not by upload batch."
      >
        <Dropzone onFiles={handleFiles} />
      </Card>

      {queue.length > 0 && (
        <Card title="Processing Queue" subtitle="This session's in-flight and just-completed uploads">
          <div>
            {queue.map((row, i) => {
              const { status, errorMessage } = effectiveQueueState(row)
              return (
                <div key={i} className="queue-row">
                  <div>
                    <div className="queue-filename">{row.filename}</div>
                    <QueueNote status={status} errorMessage={errorMessage} duplicateOf={row.duplicateOf} />
                    {status === 'PROCESSING' && <ProgressBar percent={row.progress} />}
                  </div>
                  <div className="queue-size">{formatBytes(row.size)}</div>
                  <div style={{ display: 'flex', alignItems: 'center', gap: 6, fontSize: 12.5, fontWeight: 600 }}>
                    <QueueStatusIcon status={status} />
                    {status}
                  </div>
                  <div />
                </div>
              )
            })}
          </div>
        </Card>
      )}

      <Card title="Uploaded Files" subtitle={tab === 'EXPENSE_REPORT' ? 'Expense Report documents' : 'Trip Report documents'}>
        {loading && <LoadingState />}
        {error && <ErrorState description="Could not load uploaded files." />}
        {!loading && !error && filteredUploads.length === 0 && (
          <EmptyState
            title={tab === 'EXPENSE_REPORT' ? 'No Expense Reports uploaded yet' : 'No Trip Reports uploaded yet'}
            description="Files you upload above will appear here."
          />
        )}
        {!loading && !error && filteredUploads.length > 0 && (
          <DataTable columns={columns} rows={filteredUploads} rowKey={(r) => r.document_id} />
        )}
      </Card>
    </div>
  )
}
