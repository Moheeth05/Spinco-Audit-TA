import { createContext, useCallback, useContext, useEffect, useMemo, useState } from 'react'
import { apiFetch, setCsrfToken } from '../api/client'

const AuthContext = createContext(null)

export function AuthProvider({ children }) {
  const [user, setUser] = useState(null)
  const [loading, setLoading] = useState(true)

  // `loading` starts true, so the initial session check (the effect below) has
  // nothing to set; an explicit refresh() marks loading itself, as before.
  const loadSession = useCallback(
    () =>
      apiFetch('/auth/me')
        .then((res) => res.json())
        .then((body) => {
          setCsrfToken(body.csrf_token)
          setUser(body.user)
        })
        .catch(() => {
          setUser(null)
          setCsrfToken(null)
        })
        .finally(() => setLoading(false)),
    []
  )

  const restore = useCallback(() => {
    setLoading(true)
    return loadSession()
  }, [loadSession])

  useEffect(() => {
    loadSession()
  }, [loadSession])

  const login = useCallback(async (email, password) => {
    const res = await apiFetch('/auth/login', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ email, password }),
    })
    const body = await res.json()
    setCsrfToken(body.csrf_token)
    setUser(body.user)
    return body.user
  }, [])

  const logout = useCallback(async () => {
    try {
      await apiFetch('/auth/logout', { method: 'POST' })
    } catch {
      // even if the network call fails, clear local state so the UI treats
      // the user as signed out and protected routes redirect to /login
    } finally {
      setCsrfToken(null)
      setUser(null)
    }
  }, [])

  const value = useMemo(
    () => ({
      user,
      role: user?.role || null,
      isAuthenticated: !!user,
      loading,
      login,
      logout,
      refresh: restore,
    }),
    [user, loading, login, logout, restore]
  )

  return <AuthContext.Provider value={value}>{children}</AuthContext.Provider>
}

// eslint-disable-next-line react-refresh/only-export-components -- dev-only Fast Refresh hygiene: the hook and its provider share the context object; splitting them would change the import path of all 5 consumers
export function useAuth() {
  const ctx = useContext(AuthContext)
  if (!ctx) throw new Error('useAuth must be used within an AuthProvider')
  return ctx
}
