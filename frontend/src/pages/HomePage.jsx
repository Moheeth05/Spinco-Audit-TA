import {
  AlertTriangle,
  BarChart3,
  CheckCircle2,
  FileSpreadsheet,
  FileText,
  Map,
  ShieldCheck,
  UploadCloud,
  Wallet,
} from 'lucide-react'
import { useNavigate } from 'react-router-dom'
import { useApi } from '../api/useApi'
import { Button } from '../components/ui/Button'
import { Card } from '../components/ui/Card'
import { EmptyState } from '../components/ui/EmptyState'
import { ErrorState } from '../components/ui/ErrorState'
import { KPICard } from '../components/ui/KPICard'
import { LoadingState } from '../components/ui/LoadingState'

function formatINR(amount) {
  return `Rs.${Number(amount || 0).toLocaleString('en-IN', { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`
}

export default function HomePage() {
  const navigate = useNavigate()
  const { data: kpis, loading, error } = useApi('/dashboard/kpis')
  const { data: recent } = useApi('/dashboard/recent-uploads')

  if (loading) return <LoadingState rows={5} />
  if (error) return <ErrorState description="Could not reach the audit engine. Confirm the backend service is running." />

  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: 24 }}>
      <div className="kpi-grid">
        <KPICard label="Total Expense Reports" value={kpis.total_expense_reports} icon={FileText} />
        <KPICard label="Total Trip Reports" value={kpis.total_trip_reports} icon={Map} />
        <KPICard label="Total Claimed Amount" value={formatINR(kpis.total_claimed_amount)} icon={Wallet} />
        <KPICard label="Account Verified Reports" value={kpis.account_verified_reports} icon={CheckCircle2} />
        <KPICard label="Exception Reports" value={kpis.exception_reports} icon={AlertTriangle} />
      </div>

      <div className="grid-2">
        <Card title="Monthly Uploads" subtitle="Expense reports and trip reports by month">
          <EmptyState
            title="No uploads yet"
            description="Monthly upload volume will appear here once reports start coming in."
          />
        </Card>
        <Card title="Total Claimed Amount" subtitle="Monthly claimed amount trend">
          <EmptyState
            title="No claim data yet"
            description="The monthly claimed-amount trend will appear once expense reports are processed."
          />
        </Card>
      </div>

      <div className="grid-2">
        <Card title="Expense Category Breakdown" subtitle="Claimed amount by category">
          <EmptyState title="No category data yet" description="Category spending will appear once reports are processed." />
        </Card>

        <Card title="Recent Uploads" subtitle="Latest processed files">
          {recent && recent.length > 0 ? (
            <div style={{ display: 'flex', flexDirection: 'column', gap: 10 }}>
              {recent.map((item, i) => (
                <div key={i} style={{ fontSize: 13 }}>
                  {item.filename} - {item.person_name}
                </div>
              ))}
            </div>
          ) : (
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
        </Card>
      </div>

      <Card title="Quick Actions">
        <div style={{ display: 'flex', gap: 12, flexWrap: 'wrap' }}>
          <Button variant="primary" icon={UploadCloud} onClick={() => navigate('/upload')}>
            Upload Expense Reports
          </Button>
          <Button variant="secondary" icon={UploadCloud} onClick={() => navigate('/upload')}>
            Upload Trip Reports
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
  )
}
