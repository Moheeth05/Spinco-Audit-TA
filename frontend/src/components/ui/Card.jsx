export function Card({ title, subtitle, action, children }) {
  return (
    <div className="card">
      {(title || action) && (
        <div className="card-header">
          <div>
            {title && <div className="card-title">{title}</div>}
            {subtitle && <div className="card-subtitle">{subtitle}</div>}
          </div>
          {action}
        </div>
      )}
      <div className="card-body">{children}</div>
    </div>
  )
}
