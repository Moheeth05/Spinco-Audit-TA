import { ArrowLeft, Eye, Map } from 'lucide-react'
import { useNavigate, useParams } from 'react-router-dom'
import { useApi } from '../api/useApi'
import { Card } from '../components/ui/Card'
import { DataTable } from '../components/ui/DataTable'
import { EmptyState } from '../components/ui/EmptyState'
import { ErrorState } from '../components/ui/ErrorState'
import { LoadingState } from '../components/ui/LoadingState'
import { StatusBadge } from '../components/ui/StatusBadge'

export default function TripReportMonthPage() {
  const { month } = useParams()
  const navigate = useNavigate()
  const { data: reports, loading, error } = useApi('/trip-reports', { report_month: month })

  const monthLabel = new Date(`${month}-01T00:00:00`).toLocaleDateString('en-US', {
    month: 'long',
    year: 'numeric',
  })

  const columns = [
    {
      key: 'filename',
      header: 'File Name',
      render: (r) => `${r.original_filename || r.trip_id} - ${r.employee_name || 'Pending extraction'}`,
    },
    { key: 'employee_code', header: 'Employee ID', render: (r) => r.employee_code || 'Pending extraction' },
    { key: 'trip_id', header: 'Trip ID', render: (r) => r.trip_id || 'Pending extraction' },
    { key: 'start_date', header: 'Start Date', render: (r) => r.start_date || 'Pending extraction' },
    { key: 'end_date', header: 'End Date', render: (r) => r.end_date || 'Pending extraction' },
    { key: 'destination', header: 'Destination', render: (r) => r.destination || 'Pending extraction' },
    {
      key: 'approval_status',
      header: 'Status',
      render: (r) => (r.approval_status ? <StatusBadge status={r.approval_status} /> : 'Pending extraction'),
    },
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
    <div style={{ display: 'flex', flexDirection: 'column', gap: 16 }}>
      <button className="btn btn-secondary" style={{ width: 'fit-content' }} onClick={() => navigate('/trip-reports')}>
        <ArrowLeft size={15} /> All Months
      </button>

      <Card title={`Trip Reports - ${monthLabel}`}>
        {loading && <LoadingState />}
        {error && <ErrorState description="Could not load trip reports for this month." />}
        {!loading && !error && (!reports || reports.length === 0) && (
          <EmptyState icon={Map} title="No trip reports for this month" />
        )}
        {!loading && !error && reports && reports.length > 0 && (
          <DataTable columns={columns} rows={reports} rowKey={(r) => r.document_id} />
        )}
      </Card>
    </div>
  )
}
