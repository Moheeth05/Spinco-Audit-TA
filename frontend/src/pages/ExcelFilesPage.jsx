import { FileSpreadsheet } from 'lucide-react'
import { useApi } from '../api/useApi'
import { Card } from '../components/ui/Card'
import { DataTable } from '../components/ui/DataTable'
import { EmptyState } from '../components/ui/EmptyState'
import { ErrorState } from '../components/ui/ErrorState'
import { LoadingState } from '../components/ui/LoadingState'
import { StatusBadge } from '../components/ui/StatusBadge'

function formatINR(amount) {
  if (amount === null || amount === undefined) return '-'
  return `Rs.${Number(amount).toLocaleString('en-IN', { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`
}

const columns = [
  { key: 'report_month_label', header: 'Month' },
  { key: 'filename', header: 'Workbook' },
  { key: 'total_reports', header: 'Reports', align: 'right' },
  { key: 'total_expenses', header: 'Expenses', align: 'right' },
  { key: 'total_claimed', header: 'Claimed Amount', align: 'right', render: (r) => formatINR(r.total_claimed) },
  { key: 'status', header: 'Status', render: (r) => <StatusBadge status={r.status} /> },
]

export default function ExcelFilesPage() {
  const { data: files, loading, error } = useApi('/excel-files')

  return (
    <Card title="Excel Files" subtitle="One consolidated, continuously-updated workbook per month">
      {loading && <LoadingState />}
      {error && <ErrorState description="Could not load Excel files from the audit engine." />}
      {!loading && !error && (!files || files.length === 0) && (
        <EmptyState
          icon={FileSpreadsheet}
          title="No workbooks yet"
          description="The consolidated monthly workbook is generated automatically once expense reports are processed."
        />
      )}
      {!loading && !error && files && files.length > 0 && (
        <DataTable columns={columns} rows={files} rowKey={(r) => r.report_month} />
      )}
    </Card>
  )
}
