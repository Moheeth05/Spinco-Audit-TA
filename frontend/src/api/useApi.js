import { useCallback, useEffect, useRef, useState } from 'react'
import { apiGet } from './client'

/** Fetches a real API endpoint and tracks loading/error/data state.
 * Deliberately has no fallback/mock data path - a failed or empty
 * response is shown to the user as-is via loading/error/empty states. */
export function useApi(path, params) {
  const [data, setData] = useState(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState(null)
  const [refetchIndex, setRefetchIndex] = useState(0)
  const silentRef = useRef(false)

  useEffect(() => {
    let cancelled = false
    // A silent refetch (polling) keeps showing the current data instead of flashing the loading state.
    const silent = silentRef.current
    silentRef.current = false
    if (!silent) {
      setLoading(true)
      setError(null)
    }
    apiGet(path, params)
      .then((result) => {
        if (!cancelled) {
          setData(result)
          setError(null)
        }
      })
      .catch((err) => {
        if (!cancelled && !silent) setError(err)
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
  const refetchSilently = useCallback(() => {
    silentRef.current = true
    setRefetchIndex((i) => i + 1)
  }, [])

  return { data, loading, error, refetch, refetchSilently }
}
