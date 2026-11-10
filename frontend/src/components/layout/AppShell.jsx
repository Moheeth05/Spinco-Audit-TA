import { Outlet } from 'react-router-dom'
import { useConnectionStatus } from '../../api/connection'
import { ConnectionAlerts } from './ConnectionBanner'
import { Sidebar } from './Sidebar'
import { TopHeader } from './TopHeader'

export function AppShell() {
  // One health poll for the whole window, shared by the alert bar and the header chip.
  const connection = useConnectionStatus()
  return (
    <div className="app-shell">
      <Sidebar />
      <div className="app-main">
        <ConnectionAlerts connection={connection} />
        <TopHeader connection={connection} />
        <main className="page-content">
          <Outlet />
        </main>
      </div>
    </div>
  )
}
