import { useState } from 'react'
import type { QueryResponse } from '../types'
import ConfidenceIndicator from './ConfidenceIndicator'
import ResultsTable from './ResultsTable'
import SQLViewer from './SQLViewer'

interface Props {
  result: QueryResponse
  onModelClick?: (name: string) => void
}

export default function AssistantMessage({ result, onModelClick }: Props) {
  const [showSql, setShowSql] = useState(false)
  const { sql_result, rows, row_count, execution_time_ms } = result
  const hasSql = sql_result.sql.trim().length > 0

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
        <div className="embedded-table">
          <ResultsTable
            rows={rows}
            rowCount={row_count}
            executionTimeMs={execution_time_ms}
            warehouseName=""
          />
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
