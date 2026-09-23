import { useState } from 'react'
import { apiPost } from '../api/client'
import { useAuth } from '../auth/AuthContext'
import { Card } from '../components/ui/Card'

function Row({ label, value }) {
  return (
    <div style={{ display: 'flex', justifyContent: 'space-between', padding: '10px 0', borderBottom: '1px solid var(--gray-100)' }}>
      <span style={{ color: 'var(--gray-500)' }}>{label}</span>
      <span style={{ fontWeight: 600 }}>{value}</span>
    </div>
  )
}

function ChangePasswordForm() {
  const [currentPassword, setCurrentPassword] = useState('')
  const [newPassword, setNewPassword] = useState('')
  const [confirmPassword, setConfirmPassword] = useState('')
  const [error, setError] = useState(null)
  const [success, setSuccess] = useState(false)
  const [submitting, setSubmitting] = useState(false)

  async function handleSubmit(e) {
    e.preventDefault()
    setError(null)
    setSuccess(false)
    if (newPassword !== confirmPassword) {
      setError('New password and confirmation do not match.')
      return
    }
    setSubmitting(true)
    try {
      await apiPost('/auth/change-password', {
        current_password: currentPassword,
        new_password: newPassword,
        confirm_new_password: confirmPassword,
      })
      setSuccess(true)
      setCurrentPassword('')
      setNewPassword('')
      setConfirmPassword('')
    } catch (err) {
      setError(err.message || 'Could not change password.')
    } finally {
      setSubmitting(false)
    }
  }

  return (
    <form onSubmit={handleSubmit}>
      {success && <div className="form-success">Password changed successfully. Other sessions have been signed out.</div>}
      {error && <div className="form-error">{error}</div>}

      <div className="form-field">
        <label htmlFor="current-password">Current password</label>
        <input
          id="current-password"
          type="password"
          className="input"
          value={currentPassword}
          onChange={(e) => setCurrentPassword(e.target.value)}
          autoComplete="current-password"
          required
        />
      </div>
      <div className="form-field">
        <label htmlFor="new-password">New password</label>
        <input
          id="new-password"
          type="password"
          className="input"
          value={newPassword}
          onChange={(e) => setNewPassword(e.target.value)}
          autoComplete="new-password"
          minLength={8}
          required
        />
        <span className="form-hint">At least 8 characters.</span>
      </div>
      <div className="form-field">
        <label htmlFor="confirm-password">Confirm new password</label>
        <input
          id="confirm-password"
          type="password"
          className="input"
          value={confirmPassword}
          onChange={(e) => setConfirmPassword(e.target.value)}
          autoComplete="new-password"
          minLength={8}
          required
        />
      </div>

      <div className="form-actions">
        <button type="submit" className="btn btn-primary" disabled={submitting}>
          {submitting ? 'Changing...' : 'Change Password'}
        </button>
      </div>
    </form>
  )
}

export default function SettingsPage() {
  const { user, logout } = useAuth()

  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: 16 }}>
      <Card title="Profile" subtitle="Your account details">
        <Row label="Application" value="SPINCO TA / Travel Expense Audit & Automation System" />
        <Row label="Signed in as" value={user ? `${user.name} (${user.email})` : '-'} />
        <Row label="Role" value={user?.role || '-'} />
        <Row label="Environment" value="Local development (web)" />
        <div style={{ marginTop: 16 }}>
          <button className="btn btn-secondary" onClick={logout}>
            Log Out
          </button>
        </div>
      </Card>

      <Card title="Change Password" subtitle="Changing your password signs out every other active session">
        <ChangePasswordForm />
      </Card>
    </div>
  )
}
