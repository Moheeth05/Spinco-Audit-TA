import { ChevronRight, Eye, Map, UploadCloud } from 'lucide-react'
import { useNavigate } from 'react-router-dom'
import { useApi } from '../api/useApi'
import { Button } from '../components/ui/Button'
import { Card } from '../components/ui/Card'
import { DataTable } from '../components/ui/DataTable'
import { EmptyState } from '../components/ui/EmptyState'
import { ErrorState } from '../components/ui/ErrorState'
import { LoadingState } from '../components/ui/LoadingState'
import { StatusBadge } from '../components/ui/StatusBadge'
import { formatISTDate } from '../lib/datetime'

export default function TripReportsPage() {
  const navigate = useNavigate()
  const { data: months, loading: monthsLoading, error: monthsError } = useApi('/trip-reports/months')
  const { data: flat, loading: flatLoading, error: flatError } = useApi('/trip-reports')

  const loading = monthsLoading || flatLoading
  const error = monthsError || flatError
  const hasMonths = months && months.length > 0
  const hasAnyReports = flat && flat.length > 0

  const flatColumns = [
    { key: 'filename', header: 'File Name', render: (r) => `${r.original_filename} - ${r.employee_name || 'Pending extraction'}` },
    { key: 'trip_id', header: 'Trip ID', render: (r) => r.trip_id || 'Pending extraction' },
    { key: 'start_date', header: 'Start Date', render: (r) => r.start_date || 'Pending extraction' },
    { key: 'destination', header: 'Destination', render: (r) => r.destination || 'Pending extraction' },
    { key: 'processing_status', header: 'Upload Status', render: (r) => <StatusBadge status={r.processing_status} /> },
    { key: 'uploaded_at', header: 'Uploaded', render: (r) => formatISTDate(r.uploaded_at) },
    {
      key: 'action',
      header: 'Action',
      render: (r) => (
        <button className="icon-action" title="View" onClick={() => navigate(`/documents/${r.document_id}`)}>
          <Eye size={14} />
        </button>
      ),
    },
  ]

  return (
    <Card title="Trip Reports" subtitle="Reports are grouped by their own report month, not by upload batch">
      {loading && <LoadingState />}
      {error && <ErrorState description="Could not load trip reports from the audit engine." />}

      {!loading && !error && !hasMonths && !hasAnyReports && (
        <EmptyState
          icon={Map}
          title="No Trip Reports"
          description="Trip reports will appear here after upload."
          action={
            <Button icon={UploadCloud} onClick={() => navigate('/upload')}>
              Upload Trip Report
            </Button>
          }
        />
      )}

      {!loading && !error && hasMonths && (
        <div className="month-list">
          {months.map((m) => (
            <div key={m.report_month} className="month-row" onClick={() => navigate(`/trip-reports/${m.report_month}`)}>
              <span className="month-row-label">{m.report_month_label}</span>
              <span className="month-row-count">
                {m.report_count} report{m.report_count === 1 ? '' : 's'}
                <ChevronRight size={15} style={{ verticalAlign: 'middle', marginLeft: 6 }} />
              </span>
            </div>
          ))}
        </div>
      )}

      {!loading && !error && !hasMonths && hasAnyReports && (
        <div style={{ display: 'flex', flexDirection: 'column', gap: 14 }}>
          <div className="status-badge status-info" style={{ width: 'fit-content' }}>
            Report organization will appear after document extraction.
          </div>
          <DataTable columns={flatColumns} rows={flat} rowKey={(r) => r.document_id} />
        </div>
      )}
    </Card>
  )
}
