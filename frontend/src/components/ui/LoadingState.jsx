export function LoadingState({ rows = 4 }) {
  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: 10, padding: '8px 0' }}>
      {Array.from({ length: rows }).map((_, i) => (
        <div
          key={i}
          style={{
            height: 40,
            borderRadius: 6,
            background:
              'linear-gradient(90deg, var(--gray-100) 25%, var(--gray-50) 37%, var(--gray-100) 63%)',
            backgroundSize: '400% 100%',
            animation: 'skeleton-loading 1.4s ease infinite',
          }}
        />
      ))}
      <style>{`
        @keyframes skeleton-loading {
          0% { background-position: 100% 50%; }
          100% { background-position: 0 50%; }
        }
      `}</style>
    </div>
  )
}
