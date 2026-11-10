import { useLocation } from 'react-router-dom'
import { ConnectionChip } from './ConnectionBanner'
import { ALL_NAV } from './navConfig'
import { SearchBox } from './SearchBox'

export function TopHeader({ connection }) {
  const location = useLocation()
  const isDocumentViewer = location.pathname.startsWith('/documents/')
  const isAccessDenied = location.pathname === '/access-denied'
  const current = ALL_NAV.find((item) =>
    item.end ? location.pathname === item.to : location.pathname.startsWith(item.to) && item.to !== '/'
  )
  const title = isDocumentViewer ? 'Document Viewer' : isAccessDenied ? 'Access Denied' : current?.label || 'Home'

  return (
    <header className="top-header">
      <div className="top-header-titles">
        <div className="breadcrumb">SPINCO TA AUDIT / {title.toUpperCase()}</div>
        <div className="page-title">{title}</div>
      </div>
      <div className="top-header-actions">
        {connection && <ConnectionChip connection={connection} />}
        <SearchBox />
      </div>
    </header>
  )
}
