import { useState } from 'react'
import type { SQLResultData } from '../types'
import ConfidenceIndicator from './ConfidenceIndicator'
import SQLViewer from './SQLViewer'

interface Props {
  sqlResult: SQLResultData
  onModelClick?: (modelName: string) => void
}

export default function ExplanationPanel({ sqlResult, onModelClick }: Props) {
  const [expanded, setExpanded] = useState(false)

  return (
    <div className="explanation-panel">
      {sqlResult.confidence === 'low' && (
        <div
          className="low-confidence-banner"
          role="alert"
          aria-live="assertive"
          style={{
            background: '#fef2f2',
            border: '1px solid #dc2626',
            borderRadius: 6,
            padding: '8px 12px',
            marginBottom: 12,
            color: '#991b1b',
            fontWeight: 500,
          }}
        >
          ⚠ Low confidence — please review the SQL and verify the results.{' '}
          <em>{sqlResult.confidence_reason}</em>
        </div>
      )}

      <button
        type="button"
        className="explanation-toggle"
        onClick={() => setExpanded(e => !e)}
        aria-expanded={expanded}
        aria-controls="explanation-body"
        style={{
          background: 'none',
          border: 'none',
          cursor: 'pointer',
          fontWeight: 600,
          fontSize: '0.95rem',
          color: '#1d4ed8',
          padding: 0,
          display: 'flex',
          alignItems: 'center',
          gap: 6,
        }}
      >
        {expanded ? '▾' : '▸'} How I answered this
      </button>

      {expanded && (
        <div id="explanation-body" className="explanation-body" style={{ marginTop: 12 }}>
          <p className="explanation-text">{sqlResult.explanation}</p>

          <div className="confidence-row" style={{ display: 'flex', alignItems: 'center', gap: 8, margin: '8px 0' }}>
            <span>Confidence:</span>
            <ConfidenceIndicator confidence={sqlResult.confidence} />
            <span style={{ color: '#6b7280', fontSize: '0.85rem' }}>
              {sqlResult.confidence_reason}
            </span>
          </div>

          {sqlResult.models_used.length > 0 && (
            <div className="models-used" style={{ margin: '8px 0' }}>
              <span style={{ fontWeight: 600 }}>Models used: </span>
              {sqlResult.models_used.map(name => (
                <button
                  key={name}
                  type="button"
                  className="model-tag"
                  onClick={() => onModelClick?.(name)}
                  aria-label={`View schema for ${name}`}
                  style={{
                    display: 'inline-block',
                    background: '#eff6ff',
                    border: '1px solid #bfdbfe',
                    borderRadius: 4,
                    padding: '2px 8px',
                    margin: '2px 4px',
                    cursor: 'pointer',
                    color: '#1d4ed8',
                    fontSize: '0.85rem',
                  }}
                >
                  {name}
                </button>
              ))}
            </div>
          )}

          <SQLViewer sql={sqlResult.sql} />
        </div>
      )}
    </div>
  )
}
