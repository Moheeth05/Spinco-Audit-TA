import { createContext, useCallback, useContext, useEffect, useMemo, useState } from 'react'
import { apiFetch, setCsrfToken } from '../api/client'

const AuthContext = createContext(null)

export function AuthProvider({ children }) {
  const [user, setUser] = useState(null)
  const [loading, setLoading] = useState(true)

  const restore = useCallback(async () => {
    setLoading(true)
    try {
      const res = await apiFetch('/auth/me')
      const body = await res.json()
      setCsrfToken(body.csrf_token)
      setUser(body.user)
    } catch {
      setUser(null)
      setCsrfToken(null)
    } finally {
      setLoading(false)
    }
  }, [])

  useEffect(() => {
    restore()
  }, [restore])

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

export function useAuth() {
  const ctx = useContext(AuthContext)
  if (!ctx) throw new Error('useAuth must be used within an AuthProvider')
  return ctx
}
