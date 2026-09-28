import { useEffect, useState } from 'react'
import { apiPost } from '../api/client'
import { useAuth } from '../auth/AuthContext'
import { Card } from '../components/ui/Card'
import {
  checkForUpdates,
  downloadUpdate,
  getAppVersion,
  getBuildInfo,
  getUpdateStatus,
  installUpdate,
  isElectron,
  onUpdateStatus,
} from '../lib/updater'

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

const STATUS_LABEL = {
  idle: 'Checking for updates...',
  unconfigured: "You're using the latest version.",
  checking: 'Checking for updates...',
  available: 'Update available.',
  'up-to-date': "You're using the latest version.",
  downloading: 'Downloading update...',
  downloaded: 'Update downloaded - ready to install.',
  error: 'Update check failed.',
}

function formatBytes(n) {
  if (n === null || n === undefined) return ''
  if (n < 1024 * 1024) return `${(n / 1024).toFixed(0)} KB`
  return `${(n / (1024 * 1024)).toFixed(1)} MB`
}

function ApplicationUpdateCard() {
  const [version, setVersion] = useState(null)
  const [buildInfo, setBuildInfo] = useState(null)
  const [status, setStatus] = useState(null)
  const [busy, setBusy] = useState(false)

  useEffect(() => {
    if (!isElectron()) return
    getAppVersion().then(setVersion)
    getBuildInfo().then(setBuildInfo)
    getUpdateStatus().then(setStatus)
    const unsubscribe = onUpdateStatus(setStatus)
    return unsubscribe
  }, [])

  if (!isElectron()) {
    return (
      <Card title="Application" subtitle="Version and update information">
        <Row label="Application" value="SPINCO TA / Travel Expense Audit & Automation System" />
        <Row label="Environment" value="Local development (web)" />
      </Card>
    )
  }

  const state = status?.state || 'idle'
  const label = STATUS_LABEL[state] || state

  async function handleCheck() {
    setBusy(true)
    try {
      await checkForUpdates()
    } finally {
      setBusy(false)
    }
  }

  async function handleUpdateNow() {
    setBusy(true)
    try {
      await downloadUpdate()
    } finally {
      setBusy(false)
    }
  }

  async function handleRestart() {
    await installUpdate()
  }

  return (
    <Card title="Application" subtitle="Version and update information">
      <Row label="Application" value="SPINCO TA / Travel Expense Audit & Automation System" />
      <Row label="Environment" value="Desktop application" />
      <Row label="Current Version" value={version || '-'} />
      <Row label="Latest Version" value={status?.latestVersion || (state === 'up-to-date' || state === 'unconfigured' ? version || '-' : '-')} />
      {buildInfo && (
        <Row
          label="Build"
          value={`${buildInfo.gitCommit}${buildInfo.dirty ? ' (dirty)' : ''} - ${new Date(buildInfo.buildDate).toLocaleString()}`}
        />
      )}
      <Row label="Status" value={state === 'error' && status?.error ? `${label} (${status.error})` : label} />
      {state === 'downloading' && status?.progress && (
        <div style={{ margin: '8px 0' }}>
          <div style={{ height: 6, background: 'var(--gray-100)', borderRadius: 3, overflow: 'hidden' }}>
            <div
              style={{
                height: '100%',
                width: `${Math.round(status.progress.percent || 0)}%`,
                background: 'var(--blue-600, #2563eb)',
                transition: 'width 0.2s ease',
              }}
            />
          </div>
          <div style={{ fontSize: 12, color: 'var(--gray-500)', marginTop: 4 }}>
            {Math.round(status.progress.percent || 0)}% - {formatBytes(status.progress.transferredBytes)} / {formatBytes(status.progress.totalBytes)}
          </div>
        </div>
      )}
      <div style={{ marginTop: 16, display: 'flex', gap: 8 }}>
        {state !== 'unconfigured' && (
          <button className="btn btn-secondary" onClick={handleCheck} disabled={busy || state === 'checking' || state === 'downloading'}>
            Check for Updates
          </button>
        )}
        {state === 'available' && (
          <button className="btn btn-primary" onClick={handleUpdateNow} disabled={busy}>
            Update Now
          </button>
        )}
        {state === 'downloaded' && (
          <button className="btn btn-primary" onClick={handleRestart}>
            Restart &amp; Install
          </button>
        )}
      </div>
    </Card>
  )
}

export default function SettingsPage() {
  const { user, logout } = useAuth()

  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: 16 }}>
      <Card title="Profile" subtitle="Your account details">
        <Row label="Signed in as" value={user ? `${user.name} (${user.email})` : '-'} />
        <Row label="Role" value={user?.role || '-'} />
        <div style={{ marginTop: 16 }}>
          <button className="btn btn-secondary" onClick={logout}>
            Log Out
          </button>
        </div>
      </Card>

      <ApplicationUpdateCard />

      <Card title="Change Password" subtitle="Changing your password signs out every other active session">
        <ChangePasswordForm />
      </Card>
    </div>
  )
}
