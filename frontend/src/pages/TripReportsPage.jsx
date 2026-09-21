import { ChevronRight, Map, UploadCloud } from 'lucide-react'
import { useNavigate } from 'react-router-dom'
import { useApi } from '../api/useApi'
import { Button } from '../components/ui/Button'
import { Card } from '../components/ui/Card'
import { EmptyState } from '../components/ui/EmptyState'
import { ErrorState } from '../components/ui/ErrorState'
import { LoadingState } from '../components/ui/LoadingState'

export default function TripReportsPage() {
  const navigate = useNavigate()
  const { data: months, loading, error } = useApi('/trip-reports/months')

  return (
    <Card title="Trip Reports by Month">
      {loading && <LoadingState />}
      {error && <ErrorState description="Could not load trip report months from the audit engine." />}
      {!loading && !error && (!months || months.length === 0) && (
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
      {!loading && !error && months && months.length > 0 && (
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
    </Card>
  )
}
