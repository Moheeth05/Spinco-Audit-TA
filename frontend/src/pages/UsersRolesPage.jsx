import { Key, Pencil, Power, PowerOff, Shield, UserPlus } from 'lucide-react'
import { useState } from 'react'
import { apiPost, apiPut } from '../api/client'
import { useApi } from '../api/useApi'
import { Card } from '../components/ui/Card'
import { DataTable } from '../components/ui/DataTable'
import { ErrorState } from '../components/ui/ErrorState'
import { LoadingState } from '../components/ui/LoadingState'
import { Modal } from '../components/ui/Modal'

const ROLES = ['ADMIN', 'AUDITOR', 'VIEWER']

const ROLE_MAP = {
  ADMIN: { cls: 'status-info', label: 'Admin' },
  AUDITOR: { cls: 'status-success', label: 'Auditor' },
  VIEWER: { cls: 'status-neutral', label: 'Viewer' },
}

function RoleBadge({ role }) {
  const entry = ROLE_MAP[role] || ROLE_MAP.VIEWER
  return <span className={`status-badge ${entry.cls}`}>{entry.label}</span>
}

function StatusPill({ active }) {
  return <span className={`status-badge ${active ? 'status-success' : 'status-neutral'}`}>{active ? 'Active' : 'Inactive'}</span>
}

function formatDateTime(value) {
  if (!value) return 'Never'
  return new Date(value).toLocaleString()
}

function CreateUserModal({ onClose, onCreated }) {
  const [form, setForm] = useState({ name: '', email: '', role: 'AUDITOR', password: '' })
  const [error, setError] = useState(null)
  const [submitting, setSubmitting] = useState(false)

  async function handleSubmit(e) {
    e.preventDefault()
    setError(null)
    setSubmitting(true)
    try {
      await apiPost('/users', form)
      onCreated()
      onClose()
    } catch (err) {
      setError(err.message || 'Could not create user.')
    } finally {
      setSubmitting(false)
    }
  }

  return (
    <Modal title="Create User" onClose={onClose}>
      <form onSubmit={handleSubmit}>
        {error && <div className="form-error">{error}</div>}
        <div className="form-field">
          <label>Name</label>
          <input
            className="input"
            value={form.name}
            onChange={(e) => setForm((f) => ({ ...f, name: e.target.value }))}
            required
          />
        </div>
        <div className="form-field">
          <label>Email</label>
          <input
            type="email"
            className="input"
            value={form.email}
            onChange={(e) => setForm((f) => ({ ...f, email: e.target.value }))}
            required
          />
        </div>
        <div className="form-field">
          <label>Role</label>
          <select className="input" value={form.role} onChange={(e) => setForm((f) => ({ ...f, role: e.target.value }))}>
            {ROLES.map((r) => (
              <option key={r} value={r}>
                {r}
              </option>
            ))}
          </select>
        </div>
        <div className="form-field">
          <label>Initial password</label>
          <input
            type="password"
            className="input"
            value={form.password}
            onChange={(e) => setForm((f) => ({ ...f, password: e.target.value }))}
            minLength={8}
            required
          />
          <span className="form-hint">At least 8 characters. The user must change it at next login.</span>
        </div>
        <div className="form-actions">
          <button type="button" className="btn btn-secondary" onClick={onClose}>
            Cancel
          </button>
          <button type="submit" className="btn btn-primary" disabled={submitting}>
            {submitting ? 'Creating...' : 'Create User'}
          </button>
        </div>
      </form>
    </Modal>
  )
}

function EditUserModal({ user, onClose, onSaved }) {
  const [name, setName] = useState(user.name)
  const [email, setEmail] = useState(user.email)
  const [error, setError] = useState(null)
  const [submitting, setSubmitting] = useState(false)

  async function handleSubmit(e) {
    e.preventDefault()
    setError(null)
    setSubmitting(true)
    try {
      await apiPut(`/users/${user.id}`, { name, email })
      onSaved()
      onClose()
    } catch (err) {
      setError(err.message || 'Could not update user.')
    } finally {
      setSubmitting(false)
    }
  }

  return (
    <Modal title="Edit User" onClose={onClose}>
      <form onSubmit={handleSubmit}>
        {error && <div className="form-error">{error}</div>}
        <div className="form-field">
          <label>Name</label>
          <input className="input" value={name} onChange={(e) => setName(e.target.value)} required />
        </div>
        <div className="form-field">
          <label>Email</label>
          <input type="email" className="input" value={email} onChange={(e) => setEmail(e.target.value)} required />
        </div>
        <div className="form-actions">
          <button type="button" className="btn btn-secondary" onClick={onClose}>
            Cancel
          </button>
          <button type="submit" className="btn btn-primary" disabled={submitting}>
            {submitting ? 'Saving...' : 'Save Changes'}
          </button>
        </div>
      </form>
    </Modal>
  )
}

function ChangeRoleModal({ user, onClose, onSaved }) {
  const [role, setRole] = useState(user.role)
  const [error, setError] = useState(null)
  const [submitting, setSubmitting] = useState(false)

  async function handleSubmit(e) {
    e.preventDefault()
    setError(null)
    setSubmitting(true)
    try {
      await apiPut(`/users/${user.id}/role`, { role })
      onSaved()
      onClose()
    } catch (err) {
      setError(err.message || 'Could not change role.')
    } finally {
      setSubmitting(false)
    }
  }

  return (
    <Modal title={`Change Role - ${user.name}`} onClose={onClose}>
      <form onSubmit={handleSubmit}>
        {error && <div className="form-error">{error}</div>}
        <div className="form-field">
          <label>Role</label>
          <select className="input" value={role} onChange={(e) => setRole(e.target.value)}>
            {ROLES.map((r) => (
              <option key={r} value={r}>
                {r}
              </option>
            ))}
          </select>
        </div>
        <div className="form-actions">
          <button type="button" className="btn btn-secondary" onClick={onClose}>
            Cancel
          </button>
          <button type="submit" className="btn btn-primary" disabled={submitting || role === user.role}>
            {submitting ? 'Saving...' : 'Change Role'}
          </button>
        </div>
      </form>
    </Modal>
  )
}

function ResetPasswordModal({ user, onClose }) {
  const [newPassword, setNewPassword] = useState('')
  const [error, setError] = useState(null)
  const [success, setSuccess] = useState(false)
  const [submitting, setSubmitting] = useState(false)

  async function handleSubmit(e) {
    e.preventDefault()
    setError(null)
    setSubmitting(true)
    try {
      await apiPut(`/users/${user.id}/password`, { new_password: newPassword })
      setSuccess(true)
    } catch (err) {
      setError(err.message || 'Could not reset password.')
    } finally {
      setSubmitting(false)
    }
  }

  return (
    <Modal title={`Reset Password - ${user.name}`} onClose={onClose}>
      {success ? (
        <div>
          <div className="form-success">Password reset. The user must change it at next login.</div>
          <div className="form-actions">
            <button type="button" className="btn btn-primary" onClick={onClose}>
              Done
            </button>
          </div>
        </div>
      ) : (
        <form onSubmit={handleSubmit}>
          {error && <div className="form-error">{error}</div>}
          <div className="form-field">
            <label>New password</label>
            <input
              type="password"
              className="input"
              value={newPassword}
              onChange={(e) => setNewPassword(e.target.value)}
              minLength={8}
              required
              autoFocus
            />
            <span className="form-hint">At least 8 characters. This value is never logged or shown again.</span>
          </div>
          <div className="form-actions">
            <button type="button" className="btn btn-secondary" onClick={onClose}>
              Cancel
            </button>
            <button type="submit" className="btn btn-primary" disabled={submitting}>
              {submitting ? 'Resetting...' : 'Reset Password'}
            </button>
          </div>
        </form>
      )}
    </Modal>
  )
}

function ConfirmStatusModal({ user, onClose, onSaved }) {
  const deactivating = user.is_active
  const [error, setError] = useState(null)
  const [submitting, setSubmitting] = useState(false)

  async function handleConfirm() {
    setError(null)
    setSubmitting(true)
    try {
      await apiPut(`/users/${user.id}/status`, { is_active: !user.is_active })
      onSaved()
      onClose()
    } catch (err) {
      setError(err.message || 'Could not update status.')
    } finally {
      setSubmitting(false)
    }
  }

  return (
    <Modal title={deactivating ? 'Deactivate User' : 'Activate User'} onClose={onClose}>
      {error && <div className="form-error">{error}</div>}
      <p style={{ fontSize: 13, color: 'var(--gray-700)' }}>
        {deactivating
          ? `Deactivate ${user.name}? They will be signed out immediately and unable to sign back in until reactivated.`
          : `Reactivate ${user.name}? They will be able to sign in again.`}
      </p>
      <div className="form-actions">
        <button type="button" className="btn btn-secondary" onClick={onClose}>
          Cancel
        </button>
        <button type="button" className="btn btn-primary" onClick={handleConfirm} disabled={submitting}>
          {submitting ? 'Working...' : deactivating ? 'Deactivate' : 'Activate'}
        </button>
      </div>
    </Modal>
  )
}

export default function UsersRolesPage() {
  const { data: users, loading, error, refetch } = useApi('/users')
  const [modal, setModal] = useState(null) // { type: 'create'|'edit'|'role'|'password'|'status', user? }

  const columns = [
    { key: 'name', header: 'Name' },
    { key: 'email', header: 'Email' },
    { key: 'role', header: 'Role', render: (r) => <RoleBadge role={r.role} /> },
    { key: 'is_active', header: 'Status', render: (r) => <StatusPill active={r.is_active} /> },
    { key: 'created_at', header: 'Created', render: (r) => new Date(r.created_at).toLocaleDateString() },
    { key: 'last_login_at', header: 'Last Login', render: (r) => formatDateTime(r.last_login_at) },
    {
      key: 'actions',
      header: 'Actions',
      align: 'right',
      render: (r) => (
        <div className="row-actions">
          <button className="icon-action" title="Edit" onClick={() => setModal({ type: 'edit', user: r })}>
            <Pencil size={14} />
          </button>
          <button className="icon-action" title="Change Role" onClick={() => setModal({ type: 'role', user: r })}>
            <Shield size={14} />
          </button>
          <button className="icon-action" title="Reset Password" onClick={() => setModal({ type: 'password', user: r })}>
            <Key size={14} />
          </button>
          <button
            className={`icon-action${r.is_active ? ' is-danger' : ''}`}
            title={r.is_active ? 'Deactivate' : 'Activate'}
            onClick={() => setModal({ type: 'status', user: r })}
          >
            {r.is_active ? <PowerOff size={14} /> : <Power size={14} />}
          </button>
        </div>
      ),
    },
  ]

  return (
    <>
      <Card
        title="Users & Roles"
        subtitle="ADMIN has full access; AUDITOR can upload/validate/review; VIEWER is read-only"
        action={
          <button className="btn btn-primary" onClick={() => setModal({ type: 'create' })}>
            <UserPlus size={15} />
            Create User
          </button>
        }
      >
        {loading && <LoadingState rows={4} />}
        {error && <ErrorState description="Could not load users from the audit engine." />}
        {!loading && !error && users && <DataTable columns={columns} rows={users} rowKey={(r) => r.id} />}
      </Card>

      {modal?.type === 'create' && <CreateUserModal onClose={() => setModal(null)} onCreated={refetch} />}
      {modal?.type === 'edit' && <EditUserModal user={modal.user} onClose={() => setModal(null)} onSaved={refetch} />}
      {modal?.type === 'role' && <ChangeRoleModal user={modal.user} onClose={() => setModal(null)} onSaved={refetch} />}
      {modal?.type === 'password' && <ResetPasswordModal user={modal.user} onClose={() => setModal(null)} />}
      {modal?.type === 'status' && (
        <ConfirmStatusModal user={modal.user} onClose={() => setModal(null)} onSaved={refetch} />
      )}
    </>
  )
}
