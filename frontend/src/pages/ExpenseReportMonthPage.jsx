import { ArrowLeft, Eye, FolderClosed } from 'lucide-react'
import { useState } from 'react'
import { useNavigate, useParams, useSearchParams } from 'react-router-dom'
import { useApi } from '../api/useApi'
import { Card } from '../components/ui/Card'
import { DataTable } from '../components/ui/DataTable'
import { EmptyState } from '../components/ui/EmptyState'
import { ErrorState } from '../components/ui/ErrorState'
import { LoadingState } from '../components/ui/LoadingState'
import { StatusBadge } from '../components/ui/StatusBadge'

const FILTERS = [
  { key: 'ALL', label: 'All Reports' },
  { key: 'ACCOUNT VERIFIED', label: 'Account Verified' },
  { key: 'EXCEPTION', label: 'Exceptions' },
  { key: 'REVIEW REQUIRED', label: 'Review Required' },
]

function formatINR(amount) {
  if (amount === null || amount === undefined) return '-'
  return `Rs.${Number(amount).toLocaleString('en-IN', { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`
}

export default function ExpenseReportMonthPage() {
  const { month } = useParams()
  const navigate = useNavigate()

  const columns = [
    { key: 'filename', header: 'File Name', render: (r) => r.original_filename },
    { key: 'employee_name', header: 'Employee Name', render: (r) => r.employee_name || 'Pending extraction' },
    { key: 'employee_code', header: 'Employee ID', render: (r) => r.employee_code || 'Pending extraction' },
    { key: 'total_expenses', header: 'Expenses', align: 'right' },
    { key: 'total_claimed', header: 'Claimed Amount', align: 'right', render: (r) => formatINR(r.total_claimed) },
    { key: 'verified_expense_count', header: 'Verified', align: 'right' },
    { key: 'exception_count', header: 'Exceptions', align: 'right' },
    { key: 'review_required_count', header: 'Review Required', align: 'right' },
    {
      key: 'overall_status',
      header: 'Status',
      render: (r) => (r.overall_status ? <StatusBadge status={r.overall_status} /> : 'Pending extraction'),
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
  // Supports deep-linking from Analytics drill-down (e.g. clicking the
  // EXCEPTION segment of the report-status chart) via ?status=EXCEPTION,
  // while still defaulting to ALL for normal sidebar navigation.
  const [searchParams] = useSearchParams()
  const initialStatus = searchParams.get('status') || 'ALL'
  const [status, setStatus] = useState(initialStatus)
  const { data: reports, loading, error } = useApi('/expense-reports', { report_month: month, status })

  const monthLabel = new Date(`${month}-01T00:00:00`).toLocaleDateString('en-US', {
    month: 'long',
    year: 'numeric',
  })

  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: 16 }}>
      <button className="btn btn-secondary" style={{ width: 'fit-content' }} onClick={() => navigate('/expense-reports')}>
        <ArrowLeft size={15} /> All Months
      </button>

      <Card title={`Expense Reports - ${monthLabel}`}>
        <div className="filter-bar">
          <div className="btn-group">
            {FILTERS.map((f) => (
              <button key={f.key} className={status === f.key ? 'active' : ''} onClick={() => setStatus(f.key)}>
                {f.label}
              </button>
            ))}
          </div>
        </div>

        {loading && <LoadingState />}
        {error && <ErrorState description="Could not load expense reports for this month." />}
        {!loading && !error && (!reports || reports.length === 0) && (
          <EmptyState icon={FolderClosed} title="No reports match this filter" description="Try a different status filter or month." />
        )}
        {!loading && !error && reports && reports.length > 0 && (
          <DataTable columns={columns} rows={reports} rowKey={(r) => r.document_id} />
        )}
      </Card>
    </div>
  )
}
