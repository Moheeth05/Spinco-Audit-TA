export function KPICard({ label, value, icon: Icon }) {
  return (
    <div className="kpi-card">
      <div className="kpi-card-top">
        <div className="kpi-label">{label}</div>
        {Icon && (
          <div className="kpi-icon">
            <Icon size={16} />
          </div>
        )}
      </div>
      <div className="kpi-value">{value}</div>
    </div>
  )
}
