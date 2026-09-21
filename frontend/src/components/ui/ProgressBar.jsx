export function ProgressBar({ percent }) {
  return (
    <div style={{ background: 'var(--gray-100)', borderRadius: 999, height: 6, overflow: 'hidden' }}>
      <div
        style={{
          width: `${percent}%`,
          height: '100%',
          background: 'var(--blue-600)',
          transition: 'width 0.2s ease',
        }}
      />
    </div>
  )
}
