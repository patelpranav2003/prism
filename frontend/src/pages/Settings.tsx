import { useEffect, useState, type FormEvent } from 'react'
import type { StatusResponse } from '../types'
import { authenticate, fetchStatus, triggerRefresh } from '../api'

export default function Settings() {
  const [password, setPassword] = useState('')
  const [authenticated, setAuthenticated] = useState(false)
  const [authError, setAuthError] = useState<string | null>(null)
  const [status, setStatus] = useState<StatusResponse | null>(null)
  const [refreshState, setRefreshState] = useState<'idle' | 'refreshing' | 'error' | 'success'>('idle')
  const [refreshError, setRefreshError] = useState<string | null>(null)

  useEffect(() => {
    if (authenticated) {
      fetchStatus().then(setStatus).catch(() => {})
    }
  }, [authenticated])

  async function handleAuth(e: FormEvent) {
    e.preventDefault()
    setAuthError(null)
    try {
      const { authenticated: ok } = await authenticate(password)
      if (ok) {
        setAuthenticated(true)
      } else {
        setAuthError('Incorrect password')
      }
    } catch {
      setAuthError('Authentication failed. Please try again.')
    }
  }

  async function handleRefresh() {
    setRefreshState('refreshing')
    setRefreshError(null)
    try {
      const res = await triggerRefresh(password)
      if (res.success) {
        setRefreshState('success')
        const newStatus = await fetchStatus()
        setStatus(newStatus)
      } else {
        setRefreshState('error')
        setRefreshError(res.error ?? 'Refresh failed')
      }
    } catch (e: unknown) {
      setRefreshState('error')
      setRefreshError(e instanceof Error ? e.message : 'Refresh failed')
    }
  }

  if (!authenticated) {
    return (
      <div className="settings-page">
        <div className="settings-auth-gate">
          <h1>Settings</h1>
          <p>Enter the admin password to access settings.</p>
          <form onSubmit={handleAuth} className="auth-form">
            <label htmlFor="admin-password">Password</label>
            <input
              id="admin-password"
              type="password"
              value={password}
              onChange={e => setPassword(e.target.value)}
              aria-label="Admin password"
              autoComplete="current-password"
            />
            <button type="submit">Unlock</button>
          </form>
          {authError && (
            <p className="auth-error" role="alert" aria-live="polite">
              {authError}
            </p>
          )}
          <a href="/">← Back to home</a>
        </div>
      </div>
    )
  }

  return (
    <div className="settings-page">
      <div className="settings-content">
        <h1>Settings</h1>
        <a href="/">← Back to home</a>

        <section className="settings-section">
          <h2>Schema Status</h2>
          {status ? (
            <dl className="settings-dl">
              <dt>Cache status</dt>
              <dd>{status.cache_status}</dd>
              <dt>Model count</dt>
              <dd>{status.model_count}</dd>
              <dt>Last refreshed</dt>
              <dd>{status.last_refresh_utc ? new Date(status.last_refresh_utc).toLocaleString() : 'Never'}</dd>
            </dl>
          ) : (
            <p>Loading status…</p>
          )}
        </section>

        <section className="settings-section">
          <h2>Manual Refresh</h2>
          <p>Trigger an immediate schema refresh from the GitLab artifact store.</p>
          <button
            type="button"
            onClick={handleRefresh}
            disabled={refreshState === 'refreshing'}
            className="refresh-btn"
            aria-label="Refresh schema now"
          >
            {refreshState === 'refreshing' ? 'Refreshing…' : 'Refresh Schema Now'}
          </button>
          {refreshState === 'success' && (
            <p className="refresh-success" role="status" aria-live="polite">
              ✓ Schema refreshed successfully ({status?.model_count} models)
            </p>
          )}
          {refreshState === 'error' && refreshError && (
            <p className="refresh-error" role="alert" aria-live="assertive">
              ✗ Refresh failed: {refreshError}
            </p>
          )}
        </section>
      </div>
    </div>
  )
}
