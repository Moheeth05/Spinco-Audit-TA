/** Central-storage API calls. Every path is relative to /api (see client.js) -
 * no host, credentials or storage identifiers live in the frontend. Every new
 * field the backend adds is optional: callers must tolerate it being missing. */
import { apiGet, apiPost } from './client'
import { useApi } from './useApi'

export const CENTRAL_PATHS = {
  status: '/central/status',
  attempts: '/uploads/attempts',
  records: '/storage/records',
  jobs: '/storage/jobs',
}

/** GET /api/central/status -> { mode: 'off' | 'shadow' | 'serve' } */
export function fetchCentralStatus() {
  return apiGet(CENTRAL_PATHS.status)
}

/** GET /api/uploads/attempts?result=DUPLICATE|SUCCESS|FAILED|DEFERRED&limit=200 */
export function fetchUploadAttempts({ result, limit = 200 } = {}) {
  return apiGet(CENTRAL_PATHS.attempts, { result, limit })
}

/** Administrators only. */
export function fetchStorageRecords(state = 'all') {
  return apiGet(CENTRAL_PATHS.records, { state })
}

/** Administrators only. Rejects with the server's message (e.g. 409) when nothing can be retried. */
export function retryStorageRecord(recordId) {
  return apiPost(`${CENTRAL_PATHS.records}/${encodeURIComponent(recordId)}/retry`)
}

/** Administrators only. */
export function fetchStorageJobs() {
  return apiGet(CENTRAL_PATHS.jobs)
}

/** Administrators only. */
export function retryStorageJob(jobId) {
  return apiPost(`${CENTRAL_PATHS.jobs}/${encodeURIComponent(jobId)}/retry`)
}

/** The central storage mode: 'off' | 'shadow' | 'serve'. Anything unknown,
 * still loading, or failing to load (e.g. an older backend without the
 * endpoint) is treated as 'off' so the UI keeps its pre-central look. */
export function useCentralMode() {
  const { data } = useApi(CENTRAL_PATHS.status)
  const mode = data?.mode
  return mode === 'shadow' || mode === 'serve' ? mode : 'off'
}
