import { useCallback, useEffect, useState } from 'react'
import { apiGet } from './client'

/** Fetches a real API endpoint and tracks loading/error/data state.
 * Deliberately has no fallback/mock data path - a failed or empty
 * response is shown to the user as-is via loading/error/empty states. */
export function useApi(path, params) {
  const [data, setData] = useState(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState(null)
  const [refetchIndex, setRefetchIndex] = useState(0)

  useEffect(() => {
    let cancelled = false
    // eslint-disable-next-line react-hooks/set-state-in-effect -- resetting loading/error for a new request is intentional here
    setLoading(true)
    setError(null)
    apiGet(path, params)
      .then((result) => {
        if (!cancelled) setData(result)
      })
      .catch((err) => {
        if (!cancelled) setError(err)
      })
      .finally(() => {
        if (!cancelled) setLoading(false)
      })
    return () => {
      cancelled = true
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [path, JSON.stringify(params), refetchIndex])

  const refetch = useCallback(() => setRefetchIndex((i) => i + 1), [])

  return { data, loading, error, refetch }
}
