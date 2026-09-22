/** Compact, scrollable proportional-bar list - the practical alternative to
 * an unreadable chart when there are many categories (e.g. 29 policy
 * categories, dozens of exception types/employees). Each row shows a label,
 * a proportional bar relative to the maximum value in the list, and the
 * formatted value. Sorted order is the caller's responsibility (descending
 * by value is the convention used throughout this app). */
export function HorizontalBarList({ items, labelKey, valueKey, formatValue, color = 'var(--blue-600)', maxHeight = 360 }) {
  if (!items || items.length === 0) return null
  const max = Math.max(...items.map((i) => i[valueKey]), 1)

  return (
    <div style={{ maxHeight, overflowY: 'auto', display: 'flex', flexDirection: 'column', gap: 10, paddingRight: 4 }}>
      {items.map((item, i) => {
        const pct = Math.max(2, Math.round((item[valueKey] / max) * 100))
        return (
          <div key={i}>
            <div style={{ display: 'flex', justifyContent: 'space-between', fontSize: 12.5, marginBottom: 4 }}>
              <span style={{ color: 'var(--gray-700)', fontWeight: 500 }}>{item[labelKey]}</span>
              <span style={{ color: 'var(--gray-900)', fontWeight: 600 }}>
                {formatValue ? formatValue(item[valueKey]) : item[valueKey]}
              </span>
            </div>
            <div style={{ background: 'var(--gray-100)', borderRadius: 4, height: 7, overflow: 'hidden' }}>
              <div style={{ width: `${pct}%`, height: '100%', background: color, borderRadius: 4 }} />
            </div>
          </div>
        )
      })}
    </div>
  )
}
