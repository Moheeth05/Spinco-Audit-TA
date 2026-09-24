import { useState } from 'react'
import { useApi } from '../api/useApi'
import { Card } from '../components/ui/Card'
import { DataTable } from '../components/ui/DataTable'
import { ErrorState } from '../components/ui/ErrorState'
import { LoadingState } from '../components/ui/LoadingState'
import { formatISTDateTime } from '../lib/datetime'

const ACTIONS = [
  'AUTH_LOGIN_SUCCESS',
  'AUTH_LOGIN_FAILURE',
  'AUTH_LOGOUT',
  'USER_CREATED',
  'USER_UPDATED',
  'USER_ROLE_CHANGED',
  'USER_ACTIVATED',
  'USER_DEACTIVATED',
  'USER_PASSWORD_CHANGED',
  'ADMIN_PASSWORD_RESET',
  'DOCUMENT_UPLOADED',
  'DOCUMENT_REPROCESSED',
  'DOCUMENT_DELETED',
  'REPORT_VALIDATED',
  'REPORT_REVALIDATED',
  'BILL_REMATCHED',
  'TRIP_REMATCHED',
  'EXCEL_GENERATED',
  'EXCEL_DOWNLOADED',
]

const PAGE_SIZE = 50

const columns = [
  { key: 'created_at', header: 'Timestamp', render: (r) => formatISTDateTime(r.created_at) },
  { key: 'user_name', header: 'User', render: (r) => r.user_name || '—' },
  { key: 'action', header: 'Action' },
  { key: 'entity_type', header: 'Entity', render: (r) => (r.entity_type ? `${r.entity_type} #${r.entity_id ?? ''}` : '—') },
  { key: 'details', header: 'Details', render: (r) => r.details || '—' },
]

export default function AuditLogsPage() {
  const [page, setPage] = useState(1)
  const [userId, setUserId] = useState('')
  const [action, setAction] = useState('')
  const [dateFrom, setDateFrom] = useState('')
  const [dateTo, setDateTo] = useState('')

  const { data: users } = useApi('/users')
  const { data, loading, error } = useApi('/audit-logs', {
    page,
    page_size: PAGE_SIZE,
    user_id: userId || undefined,
    action: action || undefined,
    date_from: dateFrom || undefined,
    date_to: dateTo || undefined,
  })

  function updateFilter(setter) {
    return (e) => {
      setter(e.target.value)
      setPage(1)
    }
  }

  return (
    <Card title="Audit Logs" subtitle="Immutable record of authentication and state-changing actions">
      <div className="filter-bar">
        <select className="input" style={{ width: 200 }} value={userId} onChange={updateFilter(setUserId)}>
          <option value="">All users</option>
          {(users || []).map((u) => (
            <option key={u.id} value={u.id}>
              {u.name}
            </option>
          ))}
        </select>
        <select className="input" style={{ width: 220 }} value={action} onChange={updateFilter(setAction)}>
          <option value="">All actions</option>
          {ACTIONS.map((a) => (
            <option key={a} value={a}>
              {a}
            </option>
          ))}
        </select>
        <input type="date" className="input" style={{ width: 160 }} value={dateFrom} onChange={updateFilter(setDateFrom)} />
        <input type="date" className="input" style={{ width: 160 }} value={dateTo} onChange={updateFilter(setDateTo)} />
      </div>

      {loading && <LoadingState rows={6} />}
      {error && <ErrorState description="Could not load audit logs." />}
      {!loading && !error && data && (
        <>
          <DataTable columns={columns} rows={data.items} rowKey={(r) => r.id} />
          <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', marginTop: 16 }}>
            <span className="form-hint">
              Page {data.page} of {data.total_pages} · {data.total} total events
            </span>
            <div className="btn-group">
              <button disabled={page <= 1} onClick={() => setPage((p) => Math.max(1, p - 1))}>
                Previous
              </button>
              <button disabled={page >= data.total_pages} onClick={() => setPage((p) => Math.min(data.total_pages, p + 1))}>
                Next
              </button>
            </div>
          </div>
        </>
      )}
    </Card>
  )
}
