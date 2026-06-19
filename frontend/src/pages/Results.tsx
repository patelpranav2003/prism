import { useEffect, useState } from 'react'
import { useLocation } from 'react-router-dom'
import type { QueryResponse, SchemaModelSummary, StatusResponse } from '../types'
import { fetchSchema, fetchStatus, submitQuery } from '../api'
import ExplanationPanel from '../components/ExplanationPanel'
import ResultsTable from '../components/ResultsTable'
import SchemaExplorer from '../components/SchemaExplorer'
import SearchBar from '../components/SearchBar'

export default function Results() {
  const location = useLocation()
  const state = location.state as { result: QueryResponse; question: string } | null

  const [result, setResult] = useState<QueryResponse | null>(state?.result ?? null)
  const [, setQuestion] = useState(state?.question ?? '')
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [status, setStatus] = useState<StatusResponse | null>(null)
  const [models, setModels] = useState<SchemaModelSummary[]>([])
  const [highlightModel, setHighlightModel] = useState<string | null>(null)
  const [showExplorer, setShowExplorer] = useState(true)

  useEffect(() => {
    fetchStatus().then(setStatus).catch(() => {})
    fetchSchema().then(setModels).catch(() => {})
  }, [])

  if (!result) {
    return (
      <div className="results-page">
        <p>No results to display. <a href="/">Ask a new question</a></p>
      </div>
    )
  }

  async function handleRefine(newQuestion: string) {
    setLoading(true)
    setError(null)
    try {
      const res = await submitQuery(newQuestion)
      setResult(res)
      setQuestion(newQuestion)
      window.scrollTo(0, 0)
    } catch (e: unknown) {
      setError(e instanceof Error ? e.message : 'Query failed. Please try again.')
    } finally {
      setLoading(false)
    }
  }

  return (
    <div className="results-page-layout">
      {showExplorer && (
        <SchemaExplorer
          models={models}
          highlightModel={highlightModel}
          onClose={() => setShowExplorer(false)}
        />
      )}

      <main className="results-main">
        <div className="results-top-bar">
          <a href="/" className="back-link">← New question</a>
          {!showExplorer && (
            <button type="button" onClick={() => setShowExplorer(true)} className="show-explorer-btn">
              Show Schema Explorer
            </button>
          )}
        </div>

        <ResultsTable
          rows={result.rows}
          rowCount={result.row_count}
          executionTimeMs={result.execution_time_ms}
          warehouseName={result.warehouse_name}
        />

        <ExplanationPanel
          sqlResult={result.sql_result}
          onModelClick={name => {
            setHighlightModel(name)
            setShowExplorer(true)
          }}
        />

        <div className="refine-section">
          <h3>Refine your question</h3>
          <SearchBar
            onSubmit={handleRefine}
            loading={loading}
            cacheStatus={status?.cache_status ?? null}
          />
          {error && (
            <div className="error-banner" role="alert">{error}</div>
          )}
        </div>
      </main>
    </div>
  )
}
