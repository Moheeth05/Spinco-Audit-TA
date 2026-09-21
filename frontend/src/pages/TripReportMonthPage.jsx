import { ArrowLeft, Map } from 'lucide-react'
import { useNavigate, useParams } from 'react-router-dom'
import { useApi } from '../api/useApi'
import { Card } from '../components/ui/Card'
import { DataTable } from '../components/ui/DataTable'
import { EmptyState } from '../components/ui/EmptyState'
import { ErrorState } from '../components/ui/ErrorState'
import { LoadingState } from '../components/ui/LoadingState'
import { StatusBadge } from '../components/ui/StatusBadge'

const columns = [
  { key: 'filename', header: 'File Name', render: (r) => `${r.filename || r.trip_id} - ${r.employee_name || 'Unknown'}` },
  { key: 'employee_code', header: 'Employee ID' },
  { key: 'trip_id', header: 'Trip ID' },
  { key: 'start_date', header: 'Start Date' },
  { key: 'end_date', header: 'End Date' },
  { key: 'destination', header: 'Destination' },
  { key: 'approval_status', header: 'Status', render: (r) => <StatusBadge status={r.approval_status} /> },
]

export default function TripReportMonthPage() {
  const { month } = useParams()
  const navigate = useNavigate()
  const { data: reports, loading, error } = useApi('/trip-reports', { report_month: month })

  const monthLabel = new Date(`${month}-01T00:00:00`).toLocaleDateString('en-US', {
    month: 'long',
    year: 'numeric',
  })

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
          <DataTable columns={columns} rows={reports} rowKey={(r) => r.id} />
        )}
      </Card>
    </div>
  )
}
