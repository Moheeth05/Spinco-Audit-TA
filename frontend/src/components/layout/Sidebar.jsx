import { NavLink } from 'react-router-dom'
import { ADMIN_NAV, PRIMARY_NAV } from './navConfig'

export function Sidebar({ user }) {
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
        {PRIMARY_NAV.map((item) => (
          <NavLink
            key={item.to}
            to={item.to}
            end={item.end}
            className={({ isActive }) => `sidebar-link${isActive ? ' active' : ''}`}
          >
            <item.icon size={16} />
            {item.label.toUpperCase()}
          </NavLink>
        ))}

        <div className="sidebar-section-label">ADMINISTRATION</div>
        {ADMIN_NAV.map((item) => (
          <NavLink
            key={item.to}
            to={item.to}
            className={({ isActive }) => `sidebar-link${isActive ? ' active' : ''}`}
          >
            <item.icon size={16} />
            {item.label.toUpperCase()}
          </NavLink>
        ))}
      </nav>

      <div className="sidebar-footer">
        <div className="sidebar-avatar">{initials}</div>
        <div style={{ minWidth: 0 }}>
          <div className="sidebar-user-name">{user?.name || 'Loading...'}</div>
          <div className="sidebar-user-role">{user?.role || ''}</div>
        </div>
      </div>
    </aside>
  )
}
