import { useState } from 'react'

interface Props {
  rows: Record<string, unknown>[]
  rowCount: number
  executionTimeMs: number
  warehouseName: string
}

type SortDir = 'asc' | 'desc' | null

function downloadCSV(rows: Record<string, unknown>[]) {
  if (rows.length === 0) return
  const headers = Object.keys(rows[0])
  const csvRows = [
    headers.join(','),
    ...rows.map(row =>
      headers.map(h => {
        const val = row[h]
        const str = val === null || val === undefined ? '' : String(val)
        return str.includes(',') || str.includes('"') || str.includes('\n')
          ? `"${str.replace(/"/g, '""')}"`
          : str
      }).join(',')
    ),
  ]
  const blob = new Blob([csvRows.join('\n')], { type: 'text/csv;charset=utf-8;' })
  const url = URL.createObjectURL(blob)
  const a = document.createElement('a')
  const ts = new Date().toISOString().replace(/[:.]/g, '-').slice(0, 19)
  a.href = url
  a.download = `prism_results_${ts}.csv`
  a.click()
  URL.revokeObjectURL(url)
}

export default function ResultsTable({ rows, rowCount, executionTimeMs, warehouseName }: Props) {
  const [sortCol, setSortCol] = useState<string | null>(null)
  const [sortDir, setSortDir] = useState<SortDir>(null)

  if (rows.length === 0) {
    return <p className="no-results">No rows returned.</p>
  }

  const columns = Object.keys(rows[0])

  function handleSort(col: string) {
    if (sortCol === col) {
      setSortDir(d => d === 'asc' ? 'desc' : d === 'desc' ? null : 'asc')
      if (sortDir === 'desc') setSortCol(null)
    } else {
      setSortCol(col)
      setSortDir('asc')
    }
  }

  const sortedRows = sortCol && sortDir
    ? [...rows].sort((a, b) => {
        const av = String(a[sortCol] ?? '')
        const bv = String(b[sortCol] ?? '')
        return sortDir === 'asc' ? av.localeCompare(bv) : bv.localeCompare(av)
      })
    : rows

  return (
    <div className="results-table-container">
      <div className="results-meta" aria-live="polite">
        <span>{rowCount} row{rowCount !== 1 ? 's' : ''}</span>
        <span>·</span>
        <span>{executionTimeMs}ms</span>
        {warehouseName && <><span>·</span><span>{warehouseName}</span></>}
        <button
          type="button"
          onClick={() => downloadCSV(rows)}
          className="csv-btn"
          aria-label="Download results as CSV"
        >
          Download CSV
        </button>
      </div>

      <div className="table-scroll-wrapper" tabIndex={0} aria-label="Query results table">
        <table className="results-table" aria-label={`${rowCount} result rows`}>
          <thead>
            <tr>
              {columns.map(col => (
                <th
                  key={col}
                  onClick={() => handleSort(col)}
                  aria-sort={
                    sortCol === col
                      ? sortDir === 'asc' ? 'ascending' : 'descending'
                      : 'none'
                  }
                  className="sortable-header"
                  tabIndex={0}
                  onKeyDown={e => e.key === 'Enter' && handleSort(col)}
                >
                  {col}
                  {sortCol === col && (
                    <span aria-hidden="true">{sortDir === 'asc' ? ' ↑' : ' ↓'}</span>
                  )}
                </th>
              ))}
            </tr>
          </thead>
          <tbody>
            {sortedRows.map((row, i) => (
              <tr key={i}>
                {columns.map(col => (
                  <td key={col}>{row[col] === null || row[col] === undefined ? '' : String(row[col])}</td>
                ))}
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  )
}
