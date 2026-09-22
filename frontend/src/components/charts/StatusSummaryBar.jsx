const COLORS = {
  'ACCOUNT VERIFIED': 'var(--success)',
  EXCEPTION: 'var(--error)',
  'REVIEW REQUIRED': 'var(--warning)',
  MATCHED: 'var(--success)',
  NO_MATCH: 'var(--error)',
  MULTIPLE_CANDIDATES: 'var(--warning)',
  REVIEW_REQUIRED: 'var(--warning)',
  NOT_PROCESSED: 'var(--neutral)',
}

/** A single segmented horizontal bar + legend showing counts and percentages
 * for a small fixed set of statuses (report status, bill match status, trip
 * match status). Purely descriptive - no health score, no ranking. */
export function StatusSummaryBar({ points, onSegmentClick }) {
  const total = points.reduce((sum, p) => sum + p.count, 0)

  return (
    <div>
      <div style={{ display: 'flex', height: 14, borderRadius: 7, overflow: 'hidden', background: 'var(--gray-100)' }}>
        {total === 0
          ? null
          : points
              .filter((p) => p.count > 0)
              .map((p) => (
                <div
                  key={p.status}
                  title={`${p.status}: ${p.count}`}
                  onClick={onSegmentClick ? () => onSegmentClick(p.status) : undefined}
                  style={{
                    width: `${(p.count / total) * 100}%`,
                    background: COLORS[p.status] || 'var(--gray-400)',
                    cursor: onSegmentClick ? 'pointer' : 'default',
                  }}
                />
              ))}
      </div>
      <div style={{ display: 'flex', flexWrap: 'wrap', gap: 18, marginTop: 14 }}>
        {points.map((p) => (
          <div
            key={p.status}
            onClick={onSegmentClick ? () => onSegmentClick(p.status) : undefined}
            style={{ display: 'flex', alignItems: 'center', gap: 8, cursor: onSegmentClick ? 'pointer' : 'default' }}
          >
            <span style={{ width: 10, height: 10, borderRadius: 3, background: COLORS[p.status] || 'var(--gray-400)' }} />
            <span style={{ fontSize: 12.5, color: 'var(--gray-700)' }}>{p.status}</span>
            <span style={{ fontSize: 12.5, fontWeight: 700, color: 'var(--gray-900)' }}>
              {p.count}
              {total > 0 && <span style={{ fontWeight: 500, color: 'var(--gray-500)' }}> ({Math.round((p.count / total) * 100)}%)</span>}
            </span>
          </div>
        ))}
      </div>
    </div>
  )
}
