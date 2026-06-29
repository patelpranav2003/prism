import { useState } from 'react'
import type { QueryResponse } from '../types'
import ConfidenceIndicator from './ConfidenceIndicator'
import ResultsTable from './ResultsTable'
import SQLViewer from './SQLViewer'
import ChartView from './ChartView'

interface Props {
  result: QueryResponse
  onModelClick?: (name: string) => void
}

export default function AssistantMessage({ result, onModelClick }: Props) {
  const { sql_result, rows, row_count, execution_time_ms, chart } = result
  const hasSql = sql_result.sql.trim().length > 0
  const hasChart = chart && chart.type !== 'none' && rows.length > 1

  const [showSql, setShowSql] = useState(false)
  const [view, setView] = useState<'chart' | 'table'>(hasChart ? 'chart' : 'table')

  return (
    <div className="assistant-card">
      <div className="assistant-card-header">
        <span className="prism-label">Prism</span>
        {hasSql && <ConfidenceIndicator confidence={sql_result.confidence} />}
      </div>

      {hasSql && sql_result.confidence === 'low' && (
        <div className="low-conf-note" role="alert">
          ⚠ Low confidence — please review the SQL and verify results before sharing.
        </div>
      )}

      <p className="answer-text">{sql_result.explanation}</p>

      {hasSql && rows.length > 0 && (
        <div className="results-section">
          {hasChart && (
            <div className="view-toggle" role="group" aria-label="Switch between chart and table">
              <button
                type="button"
                className={`toggle-btn ${view === 'chart' ? 'active' : ''}`}
                onClick={() => setView('chart')}
              >
                Chart
              </button>
              <button
                type="button"
                className={`toggle-btn ${view === 'table' ? 'active' : ''}`}
                onClick={() => setView('table')}
              >
                Table
              </button>
            </div>
          )}

          {hasChart && view === 'chart' && (
            <div className="embedded-chart">
              <ChartView chart={chart} rows={rows} />
            </div>
          )}

          {(!hasChart || view === 'table') && (
            <div className="embedded-table">
              <ResultsTable
                rows={rows}
                rowCount={row_count}
                executionTimeMs={execution_time_ms}
                warehouseName=""
              />
            </div>
          )}
        </div>
      )}

      {hasSql && rows.length === 0 && execution_time_ms > 0 && (
        <p style={{ color: '#6b7280', fontSize: '0.88rem', marginBottom: 12 }}>
          No rows returned.
        </p>
      )}

      {(hasSql || sql_result.models_used.length > 0) && (
        <div className="message-footer">
          {hasSql && (
            <button
              type="button"
              className="view-sql-btn"
              onClick={() => setShowSql(s => !s)}
              aria-expanded={showSql}
            >
              {showSql ? '▾' : '▸'} View SQL
            </button>
          )}

          {sql_result.models_used.length > 0 && (
            <div className="message-model-tags">
              {sql_result.models_used.map(name => (
                <button
                  key={name}
                  type="button"
                  className="model-tag"
                  onClick={() => onModelClick?.(name)}
                  aria-label={`View schema for ${name}`}
                  style={{
                    background: '#eff6ff',
                    border: '1px solid #bfdbfe',
                    borderRadius: 4,
                    padding: '2px 8px',
                    cursor: 'pointer',
                    color: '#1d4ed8',
                    fontSize: '0.82rem',
                  }}
                >
                  {name}
                </button>
              ))}
            </div>
          )}
        </div>
      )}

      {hasSql && showSql && <SQLViewer sql={sql_result.sql} />}
    </div>
  )
}
