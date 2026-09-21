import { Bell, Search } from 'lucide-react'
import { useLocation } from 'react-router-dom'
import { ALL_NAV } from './navConfig'

export function TopHeader() {
  const location = useLocation()
  const isDocumentViewer = location.pathname.startsWith('/documents/')
  const current = ALL_NAV.find((item) =>
    item.end ? location.pathname === item.to : location.pathname.startsWith(item.to) && item.to !== '/'
  )
  const title = isDocumentViewer ? 'Document Viewer' : current?.label || 'Home'

  return (
    <header className="top-header">
      <div className="top-header-titles">
        <div className="breadcrumb">SPINCO TA AUDIT / {title.toUpperCase()}</div>
        <div className="page-title">{title}</div>
      </div>
      <div className="top-header-actions">
        <button className="icon-button" aria-label="Search">
          <Search size={16} />
        </button>
        <button className="icon-button" aria-label="Notifications">
          <Bell size={16} />
        </button>
      </div>
    </header>
  )
}
