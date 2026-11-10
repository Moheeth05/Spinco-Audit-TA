/** Connection status of the backend this window talks to (local desktop backend or the central company server).
 *
 * Uses the unauthenticated GET /api/health, which reports only what KIND of backend answered
 * ({ status, environment, central_storage }) - no secret, no path, no storage identifier.
 * `window.spincoApp` exists only inside the Electron shell; every use is guarded. */
import { useCallback, useEffect, useRef, useState } from 'react'

const POLL_MS = 20000
const PROBE_TIMEOUT_MS = 5000
// A single missed probe is a blip (a busy server, a Wi-Fi hiccup): only a second consecutive miss raises the alert.
const FAILURES_BEFORE_ALERT = 2

/** One health probe. Never throws. */
export async function probeHealth(timeoutMs = PROBE_TIMEOUT_MS) {
  const controller = new AbortController()
  const timer = setTimeout(() => controller.abort(), timeoutMs)
  try {
    const res = await fetch('/api/health', { cache: 'no-store', signal: controller.signal })
    if (!res.ok) return { state: 'error', httpStatus: res.status }
    const body = await res.json()
    if (body?.status !== 'ok') return { state: 'error', httpStatus: res.status }
    return { state: 'connected', environment: String(body.environment || 'unspecified'), centralStorage: String(body.central_storage || 'off') }
  } catch {
    return { state: 'unreachable' }
  } finally {
    clearTimeout(timer)
  }
}

/** Normalises the names the backend and the desktop settings file use: dev == development. */
export function normalizeEnvironment(value) {
  const v = String(value || '').toLowerCase()
  if (v === 'dev') return 'development'
  return v
}

const ENV_LABELS = {
  local: 'Local',
  development: 'Development',
  staging: 'Staging',
  production: 'Production',
  unspecified: 'Central server',
}

export function environmentLabel(environment) {
  return ENV_LABELS[normalizeEnvironment(environment)] || 'Central server'
}

/** What the desktop shell says about its own connection ({ mode: 'local' | 'remote', host, environment, insecure }). Outside Electron: null. */
export async function readShellConnection() {
  try {
    if (window.spincoApp?.getConnectionInfo) return await window.spincoApp.getConnectionInfo()
  } catch {
    // an older shell without this call
  }
  return null
}

/** Polls the backend and exposes { status, environment, centralStorage, failures, shell, retry }.
 * `status`: 'checking' (first probe pending) | 'connected' | 'reconnecting' (one missed probe) | 'unreachable' (two or more) | 'error' (answered, but not healthy). */
export function useConnectionStatus() {
  const [health, setHealth] = useState({ state: 'checking' })
  const [failures, setFailures] = useState(0)
  const [shell, setShell] = useState(null)
  const alive = useRef(true)

  const check = useCallback(async () => {
    const result = await probeHealth()
    if (!alive.current) return
    setHealth(result)
    setFailures((n) => (result.state === 'connected' ? 0 : n + 1))
  }, [])

  useEffect(() => {
    alive.current = true
    readShellConnection().then((info) => alive.current && setShell(info))
    const first = setTimeout(check, 0)
    const timer = setInterval(check, POLL_MS)
    const onOnline = () => check()
    window.addEventListener('online', onOnline)
    window.addEventListener('offline', onOnline)
    return () => {
      alive.current = false
      clearTimeout(first)
      clearInterval(timer)
      window.removeEventListener('online', onOnline)
      window.removeEventListener('offline', onOnline)
    }
  }, [check])

  let status = health.state
  if (health.state === 'unreachable' || health.state === 'error') {
    status = failures >= FAILURES_BEFORE_ALERT ? (health.state === 'error' ? 'error' : 'unreachable') : 'reconnecting'
  }
  return { status, environment: health.environment ?? null, centralStorage: health.centralStorage ?? null, failures, shell, retry: check }
}
