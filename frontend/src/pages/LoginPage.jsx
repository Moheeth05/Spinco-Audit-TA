import { Eye, EyeOff, Lock, Mail, ShieldCheck } from 'lucide-react'
import { useState } from 'react'
import { Navigate, useLocation, useNavigate } from 'react-router-dom'
import { useConnectionStatus } from '../api/connection'
import { useAuth } from '../auth/AuthContext'
import { ConnectionChip } from '../components/layout/ConnectionBanner'

/** Says what actually went wrong. A rejected password stays deliberately generic (it never reveals whether the account exists); the other causes are not secrets. */
function loginErrorMessage(err) {
  const status = err?.status
  if (status === 401) return 'Invalid email or password.'
  if (status === 429) return err.message || 'Too many failed sign-in attempts. Try again in a few minutes.'
  if (status === 503) return 'The server is busy or not ready. Please try again in a moment.'
  if (status >= 500) return 'The server reported an error. Please try again, or contact your administrator.'
  if (status === undefined) return 'Cannot reach the server. Check your network connection (and VPN, if you use one) and try again.'
  return 'Sign-in failed. Please try again.'
}

export default function LoginPage() {
  const { isAuthenticated, loading, login } = useAuth()
  const navigate = useNavigate()
  const location = useLocation()

  const [email, setEmail] = useState('')
  const [password, setPassword] = useState('')
  const [showPassword, setShowPassword] = useState(false)
  const [error, setError] = useState(null)
  const [submitting, setSubmitting] = useState(false)
  const connection = useConnectionStatus()

  if (!loading && isAuthenticated) {
    const redirectTo = location.state?.from?.pathname || '/'
    return <Navigate to={redirectTo} replace />
  }

  async function handleSubmit(e) {
    e.preventDefault()
    setError(null)
    setSubmitting(true)
    try {
      await login(email.trim(), password)
      navigate(location.state?.from?.pathname || '/', { replace: true })
    } catch (err) {
      setError(loginErrorMessage(err))
    } finally {
      setSubmitting(false)
    }
  }

  return (
    <div className="login-page">
      <div className="login-card">
        <div className="login-brand">
          <div className="login-brand-icon">
            <ShieldCheck size={22} />
          </div>
          <div>
            <div className="login-brand-name">SPINCO TECH</div>
            <div className="login-brand-sub">TA AUDIT</div>
          </div>
        </div>

        <div className="login-heading">
          <h1>Sign in</h1>
          <p>Travel Expense Audit &amp; Automation System</p>
        </div>

        <form className="login-form" onSubmit={handleSubmit}>
          <label className="login-field">
            <span>Email</span>
            <div className="login-input-wrap">
              <Mail size={15} />
              <input
                type="email"
                autoComplete="username"
                value={email}
                onChange={(e) => setEmail(e.target.value)}
                placeholder="you@spincotech.com"
                required
                autoFocus
              />
            </div>
          </label>

          <label className="login-field">
            <span>Password</span>
            <div className="login-input-wrap">
              <Lock size={15} />
              <input
                type={showPassword ? 'text' : 'password'}
                autoComplete="current-password"
                value={password}
                onChange={(e) => setPassword(e.target.value)}
                placeholder="••••••••"
                required
              />
              <button
                type="button"
                className="login-toggle-visibility"
                onClick={() => setShowPassword((v) => !v)}
                aria-label={showPassword ? 'Hide password' : 'Show password'}
              >
                {showPassword ? <EyeOff size={15} /> : <Eye size={15} />}
              </button>
            </div>
          </label>

          {error && <div className="login-error">{error}</div>}

          <button type="submit" className="btn btn-primary login-submit" disabled={submitting}>
            {submitting ? 'Signing in...' : 'Sign in'}
          </button>
        </form>
        <div className="login-connection">
          <ConnectionChip connection={connection} />
        </div>
      </div>
    </div>
  )
}
