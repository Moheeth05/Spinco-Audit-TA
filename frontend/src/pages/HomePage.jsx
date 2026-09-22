import {
  AlertTriangle,
  BarChart3,
  CheckCircle2,
  Clock,
  Eye,
  FileSpreadsheet,
  FileText,
  Map,
  Receipt,
  ShieldCheck,
  UploadCloud,
  Wallet,
} from 'lucide-react'
import { useNavigate } from 'react-router-dom'
import { HorizontalBarList } from '../components/charts/HorizontalBarList'
import { MonthlyVolumeChart } from '../components/charts/MonthlyVolumeChart'
import { TrendLineChart } from '../components/charts/TrendLineChart'
import { useApi } from '../api/useApi'
import { Button } from '../components/ui/Button'
import { Card } from '../components/ui/Card'
import { EmptyState } from '../components/ui/EmptyState'
import { ErrorState } from '../components/ui/ErrorState'
import { KPICard } from '../components/ui/KPICard'
import { LoadingState } from '../components/ui/LoadingState'
import { StatusBadge } from '../components/ui/StatusBadge'

function formatINR(amount) {
  return `Rs.${Number(amount || 0).toLocaleString('en-IN', { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`
}

function formatINRCompact(amount) {
  return `Rs.${Number(amount || 0).toLocaleString('en-IN', { maximumFractionDigits: 0 })}`
}

export default function HomePage() {
  const navigate = useNavigate()
  const { data: kpis, loading: kpisLoading, error: kpisError } = useApi('/dashboard/kpis')
  const { data: monthlyUploads, loading: uploadsLoading, error: uploadsError } = useApi('/dashboard/monthly-uploads')
  const { data: monthlyClaimed, loading: claimedLoading, error: claimedError } = useApi('/dashboard/monthly-claimed')
  const { data: categories, loading: categoriesLoading, error: categoriesError } = useApi('/dashboard/category-breakdown')
  const { data: recent, loading: recentLoading, error: recentError } = useApi('/dashboard/recent-uploads')

  if (kpisLoading) return <LoadingState rows={5} />
  if (kpisError) return <ErrorState description="Could not reach the audit engine. Confirm the backend service is running." />

  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: 24 }}>
      <div className="kpi-grid">
        <KPICard label="Total Expense Reports" value={kpis.total_expense_reports} icon={FileText} />
        <KPICard label="Total Trip Reports" value={kpis.total_trip_reports} icon={Map} />
        <KPICard label="Account Verified" value={kpis.account_verified_reports} icon={CheckCircle2} />
        <KPICard label="Exceptions" value={kpis.exception_reports} icon={AlertTriangle} />
        <KPICard label="Review Required" value={kpis.review_required_reports} icon={Clock} />
        <KPICard label="Total Claimed Amount" value={formatINR(kpis.total_claimed_amount)} icon={Wallet} />
        <KPICard label="Total Expenses" value={kpis.total_expenses} icon={Receipt} />
      </div>

      <div className="grid-2">
        <Card title="Monthly Uploads" subtitle="Expense Reports vs Trip Reports, by report month">
          {uploadsLoading && <LoadingState rows={3} />}
          {uploadsError && <ErrorState title="Could not load upload trends" description="The server may be unreachable." />}
          {!uploadsLoading && !uploadsError && (!monthlyUploads || monthlyUploads.length === 0) && (
            <EmptyState title="No uploads yet" description="Monthly upload volume will appear here once reports start coming in." />
          )}
          {!uploadsLoading && !uploadsError && monthlyUploads && monthlyUploads.length > 0 && (
            <MonthlyVolumeChart data={monthlyUploads} />
          )}
        </Card>

        <Card title="Total Claimed Amount" subtitle="Monthly claimed amount trend (report's own stated total)">
          {claimedLoading && <LoadingState rows={3} />}
          {claimedError && <ErrorState title="Could not load claimed-amount trend" description="The server may be unreachable." />}
          {!claimedLoading && !claimedError && (!monthlyClaimed || monthlyClaimed.length === 0) && (
            <EmptyState title="No claim data yet" description="The monthly claimed-amount trend will appear once expense reports are processed." />
          )}
          {!claimedLoading && !claimedError && monthlyClaimed && monthlyClaimed.length > 0 && (
            <TrendLineChart data={monthlyClaimed} dataKey="claimed_amount" valueFormatter={formatINRCompact} />
          )}
        </Card>
      </div>

      <Card title="Expense Category Breakdown" subtitle="Claimed amount by category, highest first">
        {categoriesLoading && <LoadingState rows={4} />}
        {categoriesError && <ErrorState title="Could not load category breakdown" description="The server may be unreachable." />}
        {!categoriesLoading && !categoriesError && (!categories || categories.length === 0) && (
          <EmptyState title="No category data yet" description="Category spending will appear once reports are processed." />
        )}
        {!categoriesLoading && !categoriesError && categories && categories.length > 0 && (
          <HorizontalBarList items={categories} labelKey="category" valueKey="claimed_amount" formatValue={formatINR} />
        )}
      </Card>

      <div className="grid-2">
        <Card title="Recent Uploads" subtitle="Latest processed files">
          {recentLoading && <LoadingState rows={4} />}
          {recentError && <ErrorState title="Could not load recent uploads" description="The server may be unreachable." />}
          {!recentLoading && !recentError && (!recent || recent.length === 0) && (
            <EmptyState
              icon={UploadCloud}
              title="No Expense Reports"
              description="Upload your first Expense Report to begin auditing."
              action={
                <Button icon={UploadCloud} onClick={() => navigate('/upload')}>
                  Upload Expense Report
                </Button>
              }
            />
          )}
          {!recentLoading && !recentError && recent && recent.length > 0 && (
            <div className="table-wrap">
              <table className="data-table">
                <thead>
                  <tr>
                    <th>Document</th>
                    <th>Type</th>
                    <th>Employee</th>
                    <th>Month</th>
                    <th>Status</th>
                    <th>Uploaded</th>
                    <th></th>
                  </tr>
                </thead>
                <tbody>
                  {recent.map((item) => (
                    <tr key={item.document_id}>
                      <td>{item.filename}</td>
                      <td>{item.file_type}</td>
                      <td>{item.person_name || '-'}</td>
                      <td>{item.report_month_label || '-'}</td>
                      <td>
                        <StatusBadge status={item.status} />
                      </td>
                      <td>{new Date(item.uploaded_at).toLocaleDateString()}</td>
                      <td>
                        <button className="icon-action" title="View" onClick={() => navigate(`/documents/${item.document_id}`)}>
                          <Eye size={14} />
                        </button>
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </Card>

        <Card title="Quick Actions">
          <div style={{ display: 'flex', gap: 12, flexWrap: 'wrap' }}>
            <Button variant="primary" icon={UploadCloud} onClick={() => navigate('/upload')}>
              Upload Expense Reports
            </Button>
            <Button variant="secondary" icon={UploadCloud} onClick={() => navigate('/upload')}>
              Upload Trip Reports
            </Button>
            <Button variant="secondary" icon={FileText} onClick={() => navigate('/expense-reports')}>
              View Expense Reports
            </Button>
            <Button variant="secondary" icon={Map} onClick={() => navigate('/trip-reports')}>
              View Trip Reports
            </Button>
            <Button variant="secondary" icon={ShieldCheck} onClick={() => navigate('/policy-master')}>
              View Policy
            </Button>
            <Button variant="secondary" icon={BarChart3} onClick={() => navigate('/analytics')}>
              View Analytics
            </Button>
            <Button variant="secondary" icon={FileSpreadsheet} onClick={() => navigate('/excel-files')}>
              View Excel Files
            </Button>
          </div>
        </Card>
      </div>
    </div>
  )
}
