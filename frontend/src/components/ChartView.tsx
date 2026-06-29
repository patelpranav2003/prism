import type { ReactNode } from 'react'
import {
  BarChart, Bar,
  LineChart, Line,
  AreaChart, Area,
  PieChart, Pie, Cell,
  ScatterChart, Scatter,
  XAxis, YAxis, CartesianGrid, Tooltip, Legend,
  ResponsiveContainer,
} from 'recharts'
import type { ChartSuggestion } from '../types'

interface Props {
  chart: ChartSuggestion
  rows: Record<string, unknown>[]
}

const COLORS = ['#6366f1', '#06b6d4', '#f59e0b', '#10b981', '#f43f5e', '#8b5cf6']

function toNum(val: unknown): number {
  if (typeof val === 'number') return val
  const n = parseFloat(String(val ?? '').replace(',', ''))
  return isNaN(n) ? 0 : n
}

function fmtTick(v: number): string {
  if (v >= 1_000_000) return `${(v / 1_000_000).toFixed(1)}M`
  if (v >= 1_000) return `${(v / 1_000).toFixed(0)}K`
  return String(v)
}

const _MONTHS = ['Jan','Feb','Mar','Apr','May','Jun','Jul','Aug','Sep','Oct','Nov','Dec']

function fmtDateTick(val: unknown): string {
  const s = String(val ?? '')
  // Match YYYY-MM-DD (with optional time/timezone suffix)
  const m = s.match(/^(\d{4})-(\d{2})-(\d{2})/)
  if (m) {
    const year = parseInt(m[1])
    const month = parseInt(m[2]) - 1  // 0-indexed
    const day = parseInt(m[3])
    const mon = _MONTHS[month] ?? m[2]
    return day === 1 ? `${mon} ${year}` : `${mon} ${day}`
  }
  // Match YYYY-MM (year-month only)
  const ym = s.match(/^(\d{4})-(\d{2})$/)
  if (ym) {
    const mon = _MONTHS[parseInt(ym[2]) - 1] ?? ym[2]
    return `${mon} ${ym[1]}`
  }
  return s.length > 10 ? s.slice(0, 10) : s
}

function fmtTooltip(v: unknown): string {
  const n = typeof v === 'number' ? v : toNum(v)
  return n.toLocaleString()
}

const MIN_PX_PER_POINT = 60   // minimum pixels per bar/tick when scrolling
const SCROLL_THRESHOLD = 10   // start horizontal scroll beyond this many points
const ROTATE_THRESHOLD = 6    // rotate X labels beyond this many points

function scrollWrapper(numPoints: number, children: ReactNode) {
  const scrollable = numPoints > SCROLL_THRESHOLD
  const innerWidth = scrollable ? Math.max(numPoints * MIN_PX_PER_POINT, 600) : undefined
  return (
    <div style={{ overflowX: scrollable ? 'auto' : 'visible', width: '100%' }}>
      <div style={scrollable ? { width: innerWidth, minWidth: '100%' } : { width: '100%' }}>
        {children}
      </div>
    </div>
  )
}

export default function ChartView({ chart, rows }: Props) {
  if (chart.type === 'none' || !chart.x_column || chart.y_columns.length === 0) {
    return null
  }

  const rotateX = rows.length > ROTATE_THRESHOLD
  const tickAngle = rotateX ? -40 : 0
  const bottomMargin = rotateX ? 80 : 20

  if (chart.type === 'line' || chart.type === 'area') {
    const ChartComp = chart.type === 'area' ? AreaChart : LineChart

    const data = rows.map(r => ({
      [chart.x_column!]: r[chart.x_column!],
      ...Object.fromEntries(chart.y_columns.map(c => [c, toNum(r[c])])),
    }))

    return scrollWrapper(data.length, (
      <ResponsiveContainer width="100%" height={280}>
        <ChartComp data={data} margin={{ top: 8, right: 16, left: 0, bottom: bottomMargin }}>
          <CartesianGrid strokeDasharray="3 3" stroke="#f0f0f0" />
          <XAxis
            dataKey={chart.x_column}
            tickFormatter={fmtDateTick}
            tick={{ fontSize: 11 }}
            angle={tickAngle}
            textAnchor={tickAngle !== 0 ? 'end' : 'middle'}
            interval={0}
          />
          <YAxis tickFormatter={fmtTick} tick={{ fontSize: 11 }} width={52} />
          <Tooltip formatter={fmtTooltip} />
          {chart.y_columns.length > 1 && <Legend wrapperStyle={{ fontSize: 12 }} />}
          {chart.y_columns.map((col, i) =>
            chart.type === 'area' ? (
              <Area
                key={col}
                type="monotone"
                dataKey={col}
                stroke={COLORS[i % COLORS.length]}
                fill={COLORS[i % COLORS.length] + '28'}
                strokeWidth={2}
                dot={false}
              />
            ) : (
              <Line
                key={col}
                type="monotone"
                dataKey={col}
                stroke={COLORS[i % COLORS.length]}
                strokeWidth={2}
                dot={rows.length < 60}
                activeDot={{ r: 4 }}
              />
            )
          )}
        </ChartComp>
      </ResponsiveContainer>
    ))
  }

  if (chart.type === 'bar') {
    const data = rows.map(r => ({
      [chart.x_column!]: r[chart.x_column!],
      ...Object.fromEntries(chart.y_columns.map(c => [c, toNum(r[c])])),
    }))

    return scrollWrapper(data.length, (
      <ResponsiveContainer width="100%" height={280}>
        <BarChart data={data} margin={{ top: 8, right: 16, left: 0, bottom: bottomMargin }}>
          <CartesianGrid strokeDasharray="3 3" stroke="#f0f0f0" />
          <XAxis
            dataKey={chart.x_column}
            tick={{ fontSize: 11 }}
            angle={tickAngle}
            textAnchor={tickAngle !== 0 ? 'end' : 'middle'}
            interval={0}
          />
          <YAxis tickFormatter={fmtTick} tick={{ fontSize: 11 }} width={52} />
          <Tooltip formatter={fmtTooltip} />
          {chart.y_columns.length > 1 && <Legend wrapperStyle={{ fontSize: 12 }} />}
          {chart.y_columns.map((col, i) => (
            <Bar key={col} dataKey={col} fill={COLORS[i % COLORS.length]} radius={[3, 3, 0, 0]} maxBarSize={60} />
          ))}
        </BarChart>
      </ResponsiveContainer>
    ))
  }

  if (chart.type === 'pie') {
    const pieData = rows.map(r => ({
      name: String(r[chart.x_column!] ?? ''),
      value: toNum(r[chart.y_columns[0]]),
    }))
    const total = pieData.reduce((s, d) => s + d.value, 0)

    return (
      <ResponsiveContainer width="100%" height={280}>
        <PieChart>
          <Pie
            data={pieData}
            dataKey="value"
            nameKey="name"
            cx="50%"
            cy="50%"
            outerRadius={100}
            label={({ name, value }) =>
              `${name}: ${total > 0 ? ((value / total) * 100).toFixed(1) : 0}%`
            }
            labelLine={true}
          >
            {pieData.map((_, i) => (
              <Cell key={i} fill={COLORS[i % COLORS.length]} />
            ))}
          </Pie>
          <Tooltip formatter={(v: unknown) => fmtTooltip(v)} />
        </PieChart>
      </ResponsiveContainer>
    )
  }

  if (chart.type === 'scatter') {
    const scatterData = rows.map(r => ({
      x: toNum(r[chart.x_column!]),
      y: toNum(r[chart.y_columns[0]]),
    }))

    return (
      <ResponsiveContainer width="100%" height={280}>
        <ScatterChart margin={{ top: 8, right: 16, left: 0, bottom: 20 }}>
          <CartesianGrid strokeDasharray="3 3" stroke="#f0f0f0" />
          <XAxis
            type="number"
            dataKey="x"
            name={chart.x_column}
            tickFormatter={fmtTick}
            tick={{ fontSize: 11 }}
            label={{ value: chart.x_column, position: 'insideBottom', offset: -8, fontSize: 11, fill: '#6b7280' }}
          />
          <YAxis
            type="number"
            dataKey="y"
            name={chart.y_columns[0]}
            tickFormatter={fmtTick}
            tick={{ fontSize: 11 }}
            width={52}
          />
          <Tooltip
            cursor={{ strokeDasharray: '3 3' }}
            formatter={fmtTooltip}
          />
          <Scatter data={scatterData} fill={COLORS[0]} fillOpacity={0.75} />
        </ScatterChart>
      </ResponsiveContainer>
    )
  }

  return null
}
