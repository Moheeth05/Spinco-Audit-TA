import { CloudOff, Loader2, RefreshCw, ShieldAlert, Wifi } from 'lucide-react'
import { environmentLabel, normalizeEnvironment } from '../../api/connection'

// Never relies on color alone: every state has an icon AND a text label.
const STATUS_TEXT = {
  checking: 'Checking connection',
  connected: 'Connected',
  reconnecting: 'Reconnecting',
  unreachable: 'Server unreachable',
  error: 'Server problem',
}

function chipClass(status, environment) {
  if (status === 'unreachable' || status === 'error') return 'status-error'
  if (status === 'reconnecting' || status === 'checking') return 'status-warning'
  const env = normalizeEnvironment(environment)
  if (env === 'staging' || env === 'development') return 'status-warning'
  if (env === 'production') return 'status-info'
  return 'status-neutral'
}

/** Header chip: which environment this window is using (Local / Staging / Production ...), which server, and whether it is reachable right now. */
export function ConnectionChip({ connection }) {
  const { status, environment, shell } = connection
  const remote = shell?.mode === 'remote'
  const envName = environmentLabel(remote ? shell.environment || environment : environment || 'local')
  const where = remote && shell.host ? ` · ${shell.host}` : ''
  const Icon = status === 'connected' ? Wifi : status === 'checking' || status === 'reconnecting' ? Loader2 : CloudOff
  const text = `${envName}${where} · ${STATUS_TEXT[status] || STATUS_TEXT.checking}`
  return (
    <span className={`status-badge connection-chip ${chipClass(status, remote ? shell.environment || environment : environment)}`} title={text} aria-label={`Connection: ${text}`}>
      <Icon aria-hidden="true" />
      {text}
    </span>
  )
}

/** Full-width notices above the header: a lost connection (with a retry button), an insecure development connection, or a configuration/server environment mismatch. */
export function ConnectionAlerts({ connection }) {
  const { status, environment, shell, retry } = connection
  const remote = shell?.mode === 'remote'
  const alerts = []

  if (status === 'unreachable' || status === 'error') {
    const host = remote && shell.host ? ` (${shell.host})` : ''
    alerts.push(
      <div key="lost" className="connection-alert connection-alert-error" role="alert">
        <CloudOff aria-hidden="true" />
        <span>
          {status === 'error'
            ? `The Spinco server${host} is reachable but reports a problem. `
            : `Cannot reach the Spinco server${host}. `}
          Uploads and changes will not be saved until the connection is restored. This app does not switch to a local copy.
        </span>
        <button type="button" className="connection-alert-button" onClick={retry}>
          <RefreshCw aria-hidden="true" />
          Try again
        </button>
      </div>
    )
  }

  if (remote && shell.insecure) {
    alerts.push(
      <div key="insecure" className="connection-alert connection-alert-warning" role="status">
        <ShieldAlert aria-hidden="true" />
        <span>Insecure development connection (plain HTTP). Use synthetic documents only; do not enter real company data.</span>
      </div>
    )
  }

  if (remote && shell.environment && environment && normalizeEnvironment(shell.environment) !== normalizeEnvironment(environment)) {
    alerts.push(
      <div key="mismatch" className="connection-alert connection-alert-warning" role="status">
        <ShieldAlert aria-hidden="true" />
        <span>
          This app is set up for the {environmentLabel(shell.environment)} server, but the server answering says it is {environmentLabel(environment)}. Check the server address with your administrator before uploading anything.
        </span>
      </div>
    )
  }

  if (alerts.length === 0) return null
  return <div className="connection-alerts">{alerts}</div>
}
