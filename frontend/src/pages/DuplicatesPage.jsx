import { Link } from 'react-router-dom'
import { CENTRAL_PATHS } from '../api/central'
import { useApi } from '../api/useApi'
import { Card } from '../components/ui/Card'
import { DataTable } from '../components/ui/DataTable'
import { EmptyState } from '../components/ui/EmptyState'
import { ErrorState } from '../components/ui/ErrorState'
import { LoadingState } from '../components/ui/LoadingState'
import { formatISTLabel } from '../lib/datetime'

const TYPE_LABELS = { EXPENSE_REPORT: 'Expense Report', TRIP_REPORT: 'Trip Report' }
const ATTEMPT_PARAMS = { result: 'DUPLICATE', limit: 200 }

function formatBytes(bytes) {
  if (bytes === null || bytes === undefined) return '-'
  if (bytes < 1024) return `${bytes} B`
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`
  return `${(bytes / (1024 * 1024)).toFixed(2)} MB`
}

const columns = [
  { key: 'uploaded_at', header: 'Upload Date', render: (r) => (r.uploaded_at ? formatISTLabel(r.uploaded_at) : r.uploaded_at_ist || '-') },
  { key: 'uploaded_by_name', header: 'Uploaded By', render: (r) => r.uploaded_by_name || '-' },
  { key: 'original_filename', header: 'Uploaded File', render: (r) => r.original_filename || '-' },
  { key: 'duplicate_of_name', header: 'Original File', render: (r) => r.duplicate_of?.original_filename || '-' },
  { key: 'file_size', header: 'Size', align: 'right', render: (r) => formatBytes(r.file_size) },
  { key: 'document_type', header: 'Type', render: (r) => TYPE_LABELS[r.document_type] || r.document_type || '-' },
  { key: 'duplicate_reason', header: 'Reason', render: (r) => r.duplicate_reason || 'Identical file contents' },
  { key: 'retained_copy', header: 'Duplicate Copy', render: (r) => (r.retained_copy ? 'Retained' : 'Not stored') },
  {
    key: 'link',
    header: 'Original Document',
    render: (r) =>
      r.duplicate_of?.document_id ? <Link to={`/documents/${r.duplicate_of.document_id}`}>Open original</Link> : '-',
  },
]

export default function DuplicatesPage() {
  const { data, loading, error } = useApi(CENTRAL_PATHS.attempts, ATTEMPT_PARAMS)
  const rows = Array.isArray(data) ? data : []

  return (
    <Card title="Duplicate Uploads" subtitle="Files that were uploaded again but matched a document that already exists. Newest first, up to 200.">
      {loading && <LoadingState rows={6} />}
      {error && <ErrorState description="Could not load the duplicate history." />}
      {!loading && !error && rows.length === 0 && (
        <EmptyState title="No duplicate uploads" description="Duplicate attempts will be listed here." />
      )}
      {!loading && !error && rows.length > 0 && <DataTable columns={columns} rows={rows} rowKey={(r) => r.id} />}
    </Card>
  )
}
