import { useApi } from '../api/useApi'
import { Card } from '../components/ui/Card'
import { DataTable } from '../components/ui/DataTable'
import { ErrorState } from '../components/ui/ErrorState'
import { LoadingState } from '../components/ui/LoadingState'

const ROLE_MAP = {
  ADMIN: { cls: 'status-info', label: 'Admin' },
  AUDITOR: { cls: 'status-success', label: 'Auditor' },
  VIEWER: { cls: 'status-neutral', label: 'Viewer' },
}

function RoleBadge({ role }) {
  const entry = ROLE_MAP[role] || ROLE_MAP.VIEWER
  return <span className={`status-badge ${entry.cls}`}>{entry.label}</span>
}

const columns = [
  { key: 'name', header: 'Name' },
  { key: 'email', header: 'Email' },
  { key: 'role', header: 'Role', render: (r) => <RoleBadge role={r.role} /> },
  { key: 'created_at', header: 'Added On', render: (r) => new Date(r.created_at).toLocaleDateString() },
]

export default function UsersRolesPage() {
  const { data: users, loading, error } = useApi('/users')

  return (
    <Card title="Users & Roles" subtitle="ADMIN has full access; AUDITOR can upload/validate/review; VIEWER is read-only">
      {loading && <LoadingState rows={2} />}
      {error && <ErrorState description="Could not load users from the audit engine." />}
      {!loading && !error && users && <DataTable columns={columns} rows={users} rowKey={(r) => r.id} />}
    </Card>
  )
}
