const BASE_URL = '/api'
const SAFE_METHODS = new Set(['GET', 'HEAD', 'OPTIONS'])

let csrfToken = null
/** Called by AuthContext after login/`/auth/me` resolves (and cleared on
 * logout), so every mutating request below can attach the synchronizer-token
 * header the backend's CSRF protection requires. */
export function setCsrfToken(token) {
  csrfToken = token
}

export function getCsrfToken() {
  return csrfToken
}

function buildQuery(params) {
  if (!params) return ''
  const search = new URLSearchParams()
  Object.entries(params).forEach(([key, value]) => {
    if (value !== undefined && value !== null && value !== '') {
      search.set(key, value)
    }
  })
  const qs = search.toString()
  return qs ? `?${qs}` : ''
}

async function toError(res) {
  let detail = `Request failed with status ${res.status}`
  try {
    const body = await res.json()
    if (body?.detail) detail = body.detail
  } catch {
    // non-JSON error body - keep the generic message
  }
  const error = new Error(detail)
  error.status = res.status
  return error
}

/** Low-level fetch wrapper shared by every API module in this app (not just
 * this file) - always sends the session cookie and, for mutating methods,
 * the X-CSRF-Token header. `path` is relative to /api, e.g. "/documents/5".
 * Returns the raw Response so callers can read JSON, an ArrayBuffer, or
 * nothing (204) as appropriate. */
export async function apiFetch(path, { method = 'GET', headers, ...rest } = {}) {
  const finalHeaders = { ...headers }
  if (!SAFE_METHODS.has(method.toUpperCase()) && csrfToken) {
    finalHeaders['X-CSRF-Token'] = csrfToken
  }
  const res = await fetch(BASE_URL + path, { method, credentials: 'include', headers: finalHeaders, ...rest })
  if (!res.ok) throw await toError(res)
  return res
}

export async function apiGet(path, params) {
  const res = await apiFetch(path + buildQuery(params))
  return res.json()
}

function jsonMutation(method) {
  return async (path, body) => {
    const res = await apiFetch(path, {
      method,
      headers: { 'Content-Type': 'application/json' },
      body: body !== undefined ? JSON.stringify(body) : undefined,
    })
    if (res.status === 204) return null
    return res.json()
  }
}

export const apiPost = jsonMutation('POST')
export const apiPut = jsonMutation('PUT')
export const apiDelete = jsonMutation('DELETE')
