import { RefreshCw } from 'lucide-react'
import { useState } from 'react'
import { CENTRAL_PATHS, retryStorageJob, retryStorageRecord } from '../api/central'
import { useApi } from '../api/useApi'
import { Button } from '../components/ui/Button'
import { ProcessingStateBadge, StorageStateBadge } from '../components/ui/CentralBadges'
import { Card } from '../components/ui/Card'
import { DataTable } from '../components/ui/DataTable'
import { EmptyState } from '../components/ui/EmptyState'
import { ErrorState } from '../components/ui/ErrorState'
import { LoadingState } from '../components/ui/LoadingState'
import { formatISTLabel } from '../lib/datetime'

// Administrators only: the route is wrapped in <ProtectedRoute adminOnly /> and the
// sidebar entry lives in ADMIN_NAV; the backend enforces the same (403 otherwise).
// The API returns no Drive ids, URLs, tokens or paths, and none are rendered here -
// only short server-provided text, shown as plain strings (never raw error objects).

const ATTENTION_STATES = new Set(['UPLOADING', 'FAILED', 'MISSING_EXTERNAL'])
const MODE_LABELS = { off: 'Off', shadow: 'Shadow (copying, local files still served)', serve: 'Serve (central storage is the source of truth)' }
const RECORD_PARAMS = { state: 'all' }
const MAX_TEXT = 200

function text(value) {
  if (typeof value !== 'string' || !value) return '-'
  return value.length > MAX_TEXT ? `${value.slice(0, MAX_TEXT)}...` : value
}

function needsAttention(record) {
  return ATTENTION_STATES.has(record.state) || Boolean(record.needs_attention)
}

/** Retry button with its own busy/message state, so a 409 ("nothing can be retried") shows right beside the row. */
function RetryButton({ onRetry, onDone }) {
  const [busy, setBusy] = useState(false)
  const [message, setMessage] = useState(null)

  async function handleClick() {
    setBusy(true)
    setMessage(null)
    try {
      await onRetry()
      onDone()
    } catch (err) {
      setMessage(err?.message || 'Could not retry.')
    } finally {
      setBusy(false)
    }
  }

  return (
    <div>
      <Button variant="secondary" icon={RefreshCw} disabled={busy} onClick={handleClick}>
        {busy ? 'Retrying...' : 'Retry'}
      </Button>
      {message && <div className="form-error" style={{ marginTop: 6, whiteSpace: 'normal' }}>{message}</div>}
    </div>
  )
}

function Section({ title, subtitle, loading, error, errorText, rows, emptyTitle, columns, rowKey }) {
  return (
    <Card title={title} subtitle={subtitle}>
      {loading && <LoadingState rows={3} />}
      {error && <ErrorState description={errorText} />}
      {!loading && !error && rows.length === 0 && <EmptyState title={emptyTitle} />}
      {!loading && !error && rows.length > 0 && <DataTable columns={columns} rows={rows} rowKey={rowKey} />}
    </Card>
  )
}

export default function StorageAdminPage() {
  const status = useApi(CENTRAL_PATHS.status)
  const records = useApi(CENTRAL_PATHS.records, RECORD_PARAMS)
  const jobs = useApi(CENTRAL_PATHS.jobs)

  const mode = status.data?.mode
  const attention = (Array.isArray(records.data) ? records.data : []).filter(needsAttention)
  const jobRows = Array.isArray(jobs.data) ? jobs.data : []

  const recordColumns = [
    { key: 'original_filename', header: 'File', render: (r) => text(r.original_filename) },
    { key: 'state', header: 'Storage', render: (r) => <StorageStateBadge state={r.state === 'UPLOADING' ? 'PENDING' : r.state} /> },
    { key: 'failure_class', header: 'Failure Class', render: (r) => text(r.failure_class) },
    { key: 'attempts', header: 'Attempts', align: 'right', render: (r) => r.attempts ?? '-' },
    { key: 'deferrals', header: 'Deferrals', align: 'right', render: (r) => r.deferrals ?? '-' },
    { key: 'next_attempt_at', header: 'Next Attempt', render: (r) => formatISTLabel(r.next_attempt_at) },
    { key: 'last_error', header: 'Last Error', render: (r) => text(r.last_error || r.needs_attention) },
    {
      key: 'retry',
      header: 'Action',
      render: (r) => <RetryButton onRetry={() => retryStorageRecord(r.id)} onDone={records.refetch} />,
    },
  ]

  const jobColumns = [
    { key: 'document_id', header: 'Document', render: (r) => text(String(r.document_id ?? '')) },
    { key: 'state', header: 'State', render: (r) => <ProcessingStateBadge state={r.state} /> },
    { key: 'failure_class', header: 'Failure Class', render: (r) => text(r.failure_class) },
    { key: 'attempts', header: 'Attempts', align: 'right', render: (r) => r.attempts ?? '-' },
    { key: 'next_attempt_at', header: 'Next Attempt', render: (r) => formatISTLabel(r.next_attempt_at) },
    { key: 'last_error', header: 'Last Error', render: (r) => text(r.last_error) },
    { key: 'updated_at', header: 'Updated', render: (r) => formatISTLabel(r.updated_at) },
    {
      key: 'retry',
      header: 'Action',
      render: (r) => <RetryButton onRetry={() => retryStorageJob(r.id)} onDone={jobs.refetch} />,
    },
  ]

  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: 20 }}>
      <Card title="Central Storage" subtitle="Administrators only">
        {status.loading && <LoadingState rows={1} />}
        {status.error && <ErrorState description="Could not load the central storage mode." />}
        {!status.loading && !status.error && (
          <div>
            Mode: <strong>{MODE_LABELS[mode] || 'Unknown'}</strong>
          </div>
        )}
      </Card>

      <Section
        title="Records needing attention"
        subtitle="Copies that are still uploading, failed, or missing from central storage"
        loading={records.loading}
        error={records.error}
        errorText="Could not load storage records."
        rows={attention}
        emptyTitle="No records need attention"
        columns={recordColumns}
        rowKey={(r) => r.id}
      />

      <Section
        title="Processing jobs"
        subtitle="Jobs needing attention are listed first"
        loading={jobs.loading}
        error={jobs.error}
        errorText="Could not load processing jobs."
        rows={jobRows}
        emptyTitle="No processing jobs"
        columns={jobColumns}
        rowKey={(r) => r.id}
      />
    </div>
  )
}
