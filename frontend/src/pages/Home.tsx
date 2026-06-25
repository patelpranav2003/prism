import { useEffect, useState } from 'react'
import { useNavigate } from 'react-router-dom'
import type { SchemaModelSummary, StatusResponse } from '../types'
import { fetchSchema, fetchStatus, submitQuery } from '../api'
import SchemaHealthBar from '../components/SchemaHealthBar'
import SearchBar from '../components/SearchBar'

const POLL_INTERVAL_MS = 5000

function generateSuggestedQuestions(models: SchemaModelSummary[]): string[] {
  const gold = models.filter(m => m.layer === 'gold')
  if (gold.length === 0) return []
  const grainTemplates: Record<string, (d: string, t: string) => string> = {
    day:   (d, t) => `What are the daily ${t} for ${d}?`,
    week:  (d, t) => `Show weekly ${t} for ${d}`,
    month: (d, t) => `Show monthly ${t} for ${d}`,
    '':    (d, t) => `Show me the ${d} ${t}`,
  }
  const questions: string[] = []
  for (const m of gold) {
    if (questions.length >= 5) break
    const goldIdx = m.name.indexOf('_gold_')
    if (goldIdx === -1) continue
    const domain = m.name.slice(0, goldIdx).replace(/_/g, ' ').replace(/\b\w/g, c => c.toUpperCase())
    const rest = m.name.slice(goldIdx + 6)
    const grainSep = rest.indexOf('__')
    const topic = (grainSep !== -1 ? rest.slice(0, grainSep) : rest).replace(/_/g, ' ')
    const grain = grainSep !== -1 ? rest.slice(grainSep + 2) : ''
    const fn = grainTemplates[grain] ?? grainTemplates['']
    questions.push(fn(domain, topic))
  }
  return questions
}

export default function Home() {
  const navigate = useNavigate()
  const [status, setStatus] = useState<StatusResponse | null>(null)
  const [models, setModels] = useState<SchemaModelSummary[]>([])
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    fetchStatus().then(setStatus).catch(() => setStatus(null))
    fetchSchema().then(setModels).catch(() => {})
    const interval = setInterval(() => {
      fetchStatus().then(setStatus).catch(() => {})
    }, POLL_INTERVAL_MS)
    return () => clearInterval(interval)
  }, [])

  async function handleSubmit(question: string) {
    setLoading(true)
    setError(null)
    try {
      const result = await submitQuery(question)
      navigate('/results', { state: { result, question } })
    } catch (e: unknown) {
      setError(e instanceof Error ? e.message : 'Something went wrong. Please try again.')
    } finally {
      setLoading(false)
    }
  }

  return (
    <div className="home-page">
      <header className="home-header">
        <div className="logo-row">
          <svg width="44" height="44" viewBox="0 0 20 20" fill="none" aria-hidden="true">
            <defs>
              <linearGradient id="prism-grad-home" x1="0" y1="0" x2="1" y2="1">
                <stop offset="0%" stopColor="#818cf8"/>
                <stop offset="100%" stopColor="#1d4ed8"/>
              </linearGradient>
            </defs>
            <polygon points="10,1 19,18 1,18" fill="url(#prism-grad-home)"/>
            <line x1="10" y1="1" x2="10" y2="18" stroke="white" strokeWidth="0.8" strokeOpacity="0.4"/>
          </svg>
          <span className="logo-prism">Prism</span>
        </div>
        <p className="home-tagline">Ask anything about your data</p>
      </header>

      <main className="home-main">
        <SchemaHealthBar status={status} />

        <div className="home-search-wrapper">
          <SearchBar
            onSubmit={handleSubmit}
            loading={loading}
            cacheStatus={status?.cache_status ?? null}
            suggestedQuestions={generateSuggestedQuestions(models)}
          />
        </div>

        {error && (
          <div className="error-banner" role="alert" aria-live="assertive">
            {error}
          </div>
        )}
      </main>

      <nav className="home-nav">
        <a href="/settings" aria-label="Settings">Settings</a>
      </nav>
    </div>
  )
}
