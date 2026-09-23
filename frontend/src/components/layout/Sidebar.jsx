import { LogOut } from 'lucide-react'
import { NavLink } from 'react-router-dom'
import { useAuth } from '../../auth/AuthContext'
import { ADMIN_NAV, PRIMARY_NAV, SETTINGS_NAV } from './navConfig'

function NavSection({ items, role }) {
  return items
    .filter((item) => !item.hideFor?.includes(role))
    .map((item) => (
      <NavLink
        key={item.to}
        to={item.to}
        end={item.end}
        className={({ isActive }) => `sidebar-link${isActive ? ' active' : ''}`}
      >
        <item.icon size={16} />
        {item.label.toUpperCase()}
      </NavLink>
    ))
}

export function Sidebar() {
  const { user, role, logout } = useAuth()
  const initials = user?.name
    ? user.name
        .split(' ')
        .map((p) => p[0])
        .slice(0, 2)
        .join('')
        .toUpperCase()
    : '..'

  return (
    <aside className="sidebar">
      <div className="sidebar-brand">
        <div className="sidebar-brand-name">SPINCO TECH</div>
        <div className="sidebar-brand-sub">TA AUDIT</div>
      </div>

      <nav className="sidebar-nav">
        <NavSection items={PRIMARY_NAV} role={role} />

        <div className="sidebar-section-label">ACCOUNT</div>
        <NavSection items={SETTINGS_NAV} role={role} />

        {role === 'ADMIN' && (
          <>
            <div className="sidebar-section-label">ADMINISTRATION</div>
            <NavSection items={ADMIN_NAV} role={role} />
          </>
        )}
      </nav>

      <div className="sidebar-footer">
        <div className="sidebar-avatar">{initials}</div>
        <div style={{ minWidth: 0, flex: 1 }}>
          <div className="sidebar-user-name">{user?.name || 'Loading...'}</div>
          <div className="sidebar-user-role">{user?.role || ''}</div>
        </div>
        <button className="logout-link" onClick={logout} aria-label="Log out" title="Log out">
          <LogOut size={15} />
        </button>
      </div>
    </aside>
  )
}
