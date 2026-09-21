import { Inbox } from 'lucide-react'

export function EmptyState({ icon: Icon = Inbox, title, description, action }) {
  return (
    <div className="state-panel">
      <div className="state-icon">
        <Icon size={24} />
      </div>
      <div className="state-title">{title}</div>
      {description && <div className="state-description">{description}</div>}
      {action}
    </div>
  )
}
