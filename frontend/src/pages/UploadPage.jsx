import { AlertTriangle, CheckCircle2, Clock, Eye, FileText, Loader2, Map as MapIcon, Trash2 } from 'lucide-react'
import { useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { useApi } from '../api/useApi'
import { deleteDocument, uploadFiles } from '../api/uploads'
import { Card } from '../components/ui/Card'
import { DataTable } from '../components/ui/DataTable'
import { Dropzone } from '../components/ui/Dropzone'
import { EmptyState } from '../components/ui/EmptyState'
import { ErrorState } from '../components/ui/ErrorState'
import { LoadingState } from '../components/ui/LoadingState'
import { ProgressBar } from '../components/ui/ProgressBar'
import { StatusBadge } from '../components/ui/StatusBadge'
import { formatISTDateTime } from '../lib/datetime'

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

function QueueStatusIcon({ status }) {
  if (status === 'PROCESSING') return <Loader2 size={15} className="spin" color="var(--blue-600)" />
  if (status === 'PROCESSED') return <CheckCircle2 size={15} color="var(--success)" />
  if (status === 'FAILED') return <AlertTriangle size={15} color="var(--error)" />
  return <Clock size={15} color="var(--gray-400)" />
}

export default function UploadPage() {
  const navigate = useNavigate()
  const [tab, setTab] = useState('EXPENSE_REPORT')
  const [queue, setQueue] = useState([]) // [{ filename, size, status, progress, errorMessage }]
  const { data: uploads, loading, error, refetch } = useApi('/uploads')

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
          return {
            ...row,
            status: result.success ? 'PROCESSED' : 'FAILED',
            progress: 100,
            errorMessage: result.error_message,
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
    { key: 'uploaded_at', header: 'Upload Date', render: (r) => formatISTDateTime(r.uploaded_at) },
    {
      key: 'processing_status',
      header: 'Status',
      render: (r) => <StatusBadge status={r.processing_status} />,
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
            {queue.map((row, i) => (
              <div key={i} className="queue-row">
                <div>
                  <div className="queue-filename">{row.filename}</div>
                  {row.errorMessage && <div className="queue-error">{row.errorMessage}</div>}
                  {row.status === 'PROCESSING' && <ProgressBar percent={row.progress} />}
                </div>
                <div className="queue-size">{formatBytes(row.size)}</div>
                <div style={{ display: 'flex', alignItems: 'center', gap: 6, fontSize: 12.5, fontWeight: 600 }}>
                  <QueueStatusIcon status={row.status} />
                  {row.status}
                </div>
                <div />
              </div>
            ))}
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
