import { useEffect, useRef, useState } from 'react'
import type { Layer, SchemaModelDetail, SchemaModelSummary } from '../types'
import { fetchSchemaModel } from '../api'

interface Props {
  models: SchemaModelSummary[]
  highlightModel?: string | null
  onClose?: () => void
}

const LAYER_ORDER: Layer[] = ['gold', 'silver', 'bronze']
const LAYER_LABELS: Record<Layer, string> = { gold: '🥇 Gold', silver: '🥈 Silver', bronze: '🥉 Bronze' }

function useDebounce(value: string, delay: number) {
  const [debounced, setDebounced] = useState(value)
  useEffect(() => {
    const t = setTimeout(() => setDebounced(value), delay)
    return () => clearTimeout(t)
  }, [value, delay])
  return debounced
}

export default function SchemaExplorer({ models, highlightModel, onClose }: Props) {
  const [search, setSearch] = useState('')
  const [collapsed, setCollapsed] = useState<Record<Layer, boolean>>({ gold: false, silver: false, bronze: false })
  const [selectedModel, setSelectedModel] = useState<string | null>(null)
  const [modelDetail, setModelDetail] = useState<SchemaModelDetail | null>(null)
  const [loadingDetail, setLoadingDetail] = useState(false)
  const debouncedSearch = useDebounce(search, 300)

  useEffect(() => {
    if (highlightModel) {
      setSelectedModel(highlightModel)
    }
  }, [highlightModel])

  useEffect(() => {
    if (!selectedModel) {
      setModelDetail(null)
      return
    }
    setLoadingDetail(true)
    fetchSchemaModel(selectedModel)
      .then(setModelDetail)
      .catch(() => setModelDetail(null))
      .finally(() => setLoadingDetail(false))
  }, [selectedModel])

  const query = debouncedSearch.toLowerCase()
  const filtered = query
    ? models.filter(
        m =>
          m.name.toLowerCase().includes(query) ||
          // We don't have column names in summary, so just match on name/description
          m.description.toLowerCase().includes(query) ||
          m.fqn.toLowerCase().includes(query),
      )
    : models

  const byLayer = LAYER_ORDER.reduce((acc, layer) => {
    acc[layer] = filtered.filter(m => m.layer === layer)
    return acc
  }, {} as Record<Layer, SchemaModelSummary[]>)

  return (
    <aside className="schema-explorer" aria-label="Schema Explorer">
      <div className="schema-explorer-header">
        <h2 style={{ margin: 0, fontSize: '1rem', fontWeight: 700 }}>Schema Explorer</h2>
        {onClose && (
          <button type="button" onClick={onClose} aria-label="Close schema explorer" className="close-btn">
            ✕
          </button>
        )}
      </div>

      <div className="schema-search">
        <input
          type="search"
          placeholder="Search models…"
          value={search}
          onChange={e => setSearch(e.target.value)}
          aria-label="Search schema models"
          className="schema-search-input"
        />
      </div>

      <div className="schema-layer-list">
        {LAYER_ORDER.map(layer => {
          const layerModels = byLayer[layer]
          const isCollapsed = collapsed[layer]
          return (
            <section key={layer} className={`schema-layer schema-layer-${layer}`}>
              <button
                type="button"
                className="schema-layer-header"
                onClick={() => setCollapsed(c => ({ ...c, [layer]: !c[layer] }))}
                aria-expanded={!isCollapsed}
                aria-controls={`layer-${layer}`}
              >
                {isCollapsed ? '▸' : '▾'} {LAYER_LABELS[layer]} ({layerModels.length})
              </button>
              {!isCollapsed && (
                <ul id={`layer-${layer}`} className="schema-model-list" role="list">
                  {layerModels.map(m => (
                    <li key={m.name} role="listitem">
                      <button
                        type="button"
                        className={`schema-model-item ${selectedModel === m.name ? 'selected' : ''}`}
                        onClick={() => setSelectedModel(m.name)}
                        aria-label={`View schema for ${m.name}`}
                        aria-pressed={selectedModel === m.name}
                      >
                        <span className="model-name">{m.name}</span>
                        <span className="model-meta">{m.column_count} cols</span>
                      </button>
                    </li>
                  ))}
                  {layerModels.length === 0 && (
                    <li className="no-models-msg">No models{query ? ' matching search' : ''}</li>
                  )}
                </ul>
              )}
            </section>
          )
        })}
      </div>

      {selectedModel && (
        <div className="schema-detail-panel" aria-live="polite" aria-label={`Schema detail for ${selectedModel}`}>
          <button type="button" className="close-detail-btn" onClick={() => setSelectedModel(null)} aria-label="Close detail panel">
            ✕
          </button>
          {loadingDetail ? (
            <p>Loading…</p>
          ) : modelDetail ? (
            <ModelDetailView detail={modelDetail} />
          ) : (
            <p>Failed to load model details.</p>
          )}
        </div>
      )}
    </aside>
  )
}

function ModelDetailView({ detail }: { detail: SchemaModelDetail }) {
  return (
    <div className="model-detail">
      <h3 style={{ marginTop: 0 }}>{detail.name}</h3>
      <p className="model-fqn" style={{ color: '#6b7280', fontSize: '0.85rem' }}>{detail.fqn}</p>
      {detail.description && <p>{detail.description}</p>}
      <div className="detail-meta">
        <span><strong>Layer:</strong> {detail.layer}</span>
        <span><strong>Grain:</strong> {detail.grain}</span>
        {detail.row_count > 0 && <span><strong>Rows:</strong> {detail.row_count.toLocaleString()}</span>}
        {detail.last_updated && <span><strong>Updated:</strong> {detail.last_updated.slice(0, 10)}</span>}
      </div>

      {detail.tags.length > 0 && (
        <div className="detail-tags">
          {detail.tags.map(t => <span key={t} className="tag">{t}</span>)}
        </div>
      )}

      <h4>Columns ({detail.columns.length})</h4>
      <table className="columns-table">
        <thead>
          <tr><th>Name</th><th>Type</th><th>Description</th></tr>
        </thead>
        <tbody>
          {detail.columns.map(col => (
            <tr key={col.name}>
              <td><code>{col.name}</code></td>
              <td>{col.data_type}</td>
              <td>{col.description}</td>
            </tr>
          ))}
        </tbody>
      </table>

      {(detail.parents.length > 0 || detail.children.length > 0) && (
        <div className="detail-lineage">
          <h4>Lineage</h4>
          {detail.parents.length > 0 && <p><strong>Depends on:</strong> {detail.parents.join(', ')}</p>}
          {detail.children.length > 0 && <p><strong>Used by:</strong> {detail.children.join(', ')}</p>}
        </div>
      )}
    </div>
  )
}
