import { Bar, BarChart, CartesianGrid, Legend, ResponsiveContainer, Tooltip, XAxis, YAxis } from 'recharts'

/** Grouped bar chart: Expense Reports vs Trip Reports, by report month. */
export function MonthlyVolumeChart({ data }) {
  return (
    <ResponsiveContainer width="100%" height={260}>
      <BarChart data={data} margin={{ top: 8, right: 12, left: 0, bottom: 0 }}>
        <CartesianGrid strokeDasharray="3 3" stroke="var(--gray-200)" vertical={false} />
        <XAxis dataKey="month_label" tick={{ fontSize: 11, fill: 'var(--gray-600)' }} axisLine={{ stroke: 'var(--gray-200)' }} />
        <YAxis allowDecimals={false} tick={{ fontSize: 11, fill: 'var(--gray-600)' }} axisLine={false} tickLine={false} />
        <Tooltip
          formatter={(value, name) => [value, name === 'expense_reports' ? 'Expense Reports' : 'Trip Reports']}
          labelStyle={{ fontWeight: 600 }}
          contentStyle={{ fontSize: 12.5, borderRadius: 6, border: '1px solid var(--border)' }}
        />
        <Legend
          formatter={(v) => (v === 'expense_reports' ? 'Expense Reports' : 'Trip Reports')}
          wrapperStyle={{ fontSize: 12 }}
        />
        <Bar dataKey="expense_reports" fill="var(--blue-600)" radius={[3, 3, 0, 0]} maxBarSize={28} />
        <Bar dataKey="trip_reports" fill="var(--navy-700)" radius={[3, 3, 0, 0]} maxBarSize={28} />
      </BarChart>
    </ResponsiveContainer>
  )
}
