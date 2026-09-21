import { AlertTriangle } from 'lucide-react'

export function ErrorState({ title = 'Something went wrong', description }) {
  return (
    <div className="state-panel">
      <div className="state-icon is-error">
        <AlertTriangle size={24} />
      </div>
      <div className="state-title">{title}</div>
      {description && <div className="state-description">{description}</div>}
    </div>
  )
}
