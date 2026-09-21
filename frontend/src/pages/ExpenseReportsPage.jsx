import { ChevronRight, FolderClosed, UploadCloud } from 'lucide-react'
import { useNavigate } from 'react-router-dom'
import { useApi } from '../api/useApi'
import { Button } from '../components/ui/Button'
import { Card } from '../components/ui/Card'
import { EmptyState } from '../components/ui/EmptyState'
import { ErrorState } from '../components/ui/ErrorState'
import { LoadingState } from '../components/ui/LoadingState'

export default function ExpenseReportsPage() {
  const navigate = useNavigate()
  const { data: months, loading, error } = useApi('/expense-reports/months')

  return (
    <Card title="Expense Reports by Month" subtitle="Reports are grouped by their own report month, not by upload batch">
      {loading && <LoadingState />}
      {error && <ErrorState description="Could not load expense report months from the audit engine." />}
      {!loading && !error && (!months || months.length === 0) && (
        <EmptyState
          icon={FolderClosed}
          title="No Expense Reports"
          description="Upload your first Expense Report to begin auditing."
          action={
            <Button icon={UploadCloud} onClick={() => navigate('/upload')}>
              Upload Expense Report
            </Button>
          }
        />
      )}
      {!loading && !error && months && months.length > 0 && (
        <div className="month-list">
          {months.map((m) => (
            <div key={m.report_month} className="month-row" onClick={() => navigate(`/expense-reports/${m.report_month}`)}>
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
