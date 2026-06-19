import { useEffect, useState } from 'react'
import { useNavigate } from 'react-router-dom'
import type { StatusResponse } from '../types'
import { fetchStatus, submitQuery } from '../api'
import SchemaHealthBar from '../components/SchemaHealthBar'
import SearchBar from '../components/SearchBar'

const POLL_INTERVAL_MS = 5000

export default function Home() {
  const navigate = useNavigate()
  const [status, setStatus] = useState<StatusResponse | null>(null)
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    fetchStatus().then(setStatus).catch(() => setStatus(null))
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
          <span className="logo-zuru" aria-label="ZURU">ZURU</span>
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
