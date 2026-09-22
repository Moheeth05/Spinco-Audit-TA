import { CartesianGrid, Line, LineChart, ResponsiveContainer, Tooltip, XAxis, YAxis } from 'recharts'

/** Single-series month trend line (claimed amount / exception count / trip count). */
export function TrendLineChart({ data, dataKey, valueFormatter, color = 'var(--blue-600)' }) {
  return (
    <ResponsiveContainer width="100%" height={240}>
      <LineChart data={data} margin={{ top: 8, right: 16, left: 0, bottom: 0 }}>
        <CartesianGrid strokeDasharray="3 3" stroke="var(--gray-200)" vertical={false} />
        <XAxis dataKey="month_label" tick={{ fontSize: 11, fill: 'var(--gray-600)' }} axisLine={{ stroke: 'var(--gray-200)' }} />
        <YAxis
          allowDecimals={false}
          tick={{ fontSize: 11, fill: 'var(--gray-600)' }}
          axisLine={false}
          tickLine={false}
          tickFormatter={valueFormatter}
          width={70}
        />
        <Tooltip
          formatter={(value) => [valueFormatter ? valueFormatter(value) : value, undefined]}
          labelStyle={{ fontWeight: 600 }}
          contentStyle={{ fontSize: 12.5, borderRadius: 6, border: '1px solid var(--border)' }}
        />
        <Line type="monotone" dataKey={dataKey} stroke={color} strokeWidth={2.5} dot={{ r: 3 }} activeDot={{ r: 5 }} />
      </LineChart>
    </ResponsiveContainer>
  )
}
