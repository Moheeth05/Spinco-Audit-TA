import { Navigate, Outlet, useLocation } from 'react-router-dom'
import { useAuth } from '../../auth/AuthContext'
import { LoadingState } from '../ui/LoadingState'

/** Redirects to /login when unauthenticated. When `adminOnly` is set, also
 * redirects authenticated non-ADMIN users to /access-denied. This is a UX
 * convenience only - every route it guards also enforces the equivalent
 * permission on the backend, which remains authoritative. */
export function ProtectedRoute({ adminOnly = false }) {
  const { isAuthenticated, loading, role } = useAuth()
  const location = useLocation()

  if (loading) {
    return (
      <div style={{ padding: 48 }}>
        <LoadingState rows={3} />
      </div>
    )
  }

  if (!isAuthenticated) {
    return <Navigate to="/login" state={{ from: location }} replace />
  }

  if (adminOnly && role !== 'ADMIN') {
    return <Navigate to="/access-denied" replace />
  }

  return <Outlet />
}
