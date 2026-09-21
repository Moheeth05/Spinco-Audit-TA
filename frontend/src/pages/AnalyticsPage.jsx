import { useApi } from '../api/useApi'
import { Card } from '../components/ui/Card'
import { EmptyState } from '../components/ui/EmptyState'
import { ErrorState } from '../components/ui/ErrorState'
import { KPICard } from '../components/ui/KPICard'
import { LoadingState } from '../components/ui/LoadingState'
import { CheckCircle2, FileText, TrendingUp, Wallet } from 'lucide-react'

function formatINR(amount) {
  return `Rs.${Number(amount || 0).toLocaleString('en-IN', { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`
}

export default function AnalyticsPage() {
  const { data: summary, loading, error } = useApi('/analytics/summary')

  if (loading) return <LoadingState rows={4} />
  if (error) return <ErrorState description="Could not load analytics from the audit engine." />

  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: 24 }}>
      <div className="kpi-grid">
        <KPICard label="Total Reports" value={summary.total_reports} icon={FileText} />
        <KPICard label="Verified Reports" value={summary.verified_reports} icon={CheckCircle2} />
        <KPICard label="Verification Rate" value={`${summary.verification_rate}%`} icon={TrendingUp} />
        <KPICard label="Total Claimed" value={formatINR(summary.total_claimed)} icon={Wallet} />
      </div>

      <div className="grid-2">
        <Card title="Monthly Expense Trends">
          <EmptyState title="No trend data yet" description="Trends appear once expense reports are processed." />
        </Card>
        <Card title="Employee-wise Claimed Amount">
          <EmptyState title="No employee data yet" description="Employee spending breakdown appears once reports are processed." />
        </Card>
      </div>
    </div>
  )
}
