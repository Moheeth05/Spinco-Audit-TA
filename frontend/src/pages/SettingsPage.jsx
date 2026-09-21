import { useApi } from '../api/useApi'
import { Card } from '../components/ui/Card'
import { ErrorState } from '../components/ui/ErrorState'
import { LoadingState } from '../components/ui/LoadingState'

function Row({ label, value }) {
  return (
    <div style={{ display: 'flex', justifyContent: 'space-between', padding: '10px 0', borderBottom: '1px solid var(--gray-100)' }}>
      <span style={{ color: 'var(--gray-500)' }}>{label}</span>
      <span style={{ fontWeight: 600 }}>{value}</span>
    </div>
  )
}

export default function SettingsPage() {
  const { data: user, loading, error } = useApi('/users/me')

  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: 16 }}>
      <Card title="System" subtitle="Environment and version information">
        <Row label="Application" value="SPINCO TA / Travel Expense Audit & Automation System" />
        <Row label="Signed in as" value={user ? `${user.name} (${user.email})` : loading ? 'Loading...' : '-'} />
        <Row label="Role" value={user?.role || '-'} />
        <Row label="Environment" value="Local development (web)" />
      </Card>

      <Card title="Administration" subtitle="Full policy, upload, and workflow settings are added as later phases land">
        {error && <ErrorState description="Could not load account information." />}
        {!error && loading && <LoadingState rows={2} />}
        {!error && !loading && (
          <p style={{ fontSize: 13, color: 'var(--gray-500)' }}>
            Role-based access control, notification preferences, and integration settings will appear here as
            authentication and the processing pipeline come online.
          </p>
        )}
      </Card>
    </div>
  )
}
