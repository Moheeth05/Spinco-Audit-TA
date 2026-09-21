import { Outlet } from 'react-router-dom'
import { useApi } from '../../api/useApi'
import { Sidebar } from './Sidebar'
import { TopHeader } from './TopHeader'

export function AppShell() {
  const { data: user } = useApi('/users/me')

  return (
    <div className="app-shell">
      <Sidebar user={user} />
      <div className="app-main">
        <TopHeader />
        <main className="page-content">
          <Outlet />
        </main>
      </div>
    </div>
  )
}
