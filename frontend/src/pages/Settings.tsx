import { useEffect, useState, type FormEvent } from 'react'
import type { AppIdentityData, StatusResponse } from '../types'
import { authenticate, fetchAppIdentity, fetchStatus, saveAppIdentity, triggerRefresh } from '../api'

const EMPTY_IDENTITY: AppIdentityData = {
  owner_name: '',
  owner_title: '',
  owner_email: '',
  team_name: '',
  company_name: '',
}

export default function Settings() {
  const [password, setPassword] = useState('')
  const [authenticated, setAuthenticated] = useState(false)
  const [authError, setAuthError] = useState<string | null>(null)
  const [status, setStatus] = useState<StatusResponse | null>(null)
  const [refreshState, setRefreshState] = useState<'idle' | 'refreshing' | 'error' | 'success'>('idle')
  const [refreshError, setRefreshError] = useState<string | null>(null)

  // App Identity
  const [identity, setIdentity] = useState<AppIdentityData>(EMPTY_IDENTITY)
  const [identitySaveState, setIdentitySaveState] = useState<'idle' | 'saving' | 'saved' | 'error'>('idle')
  const [identityError, setIdentityError] = useState<string | null>(null)

  useEffect(() => {
    if (authenticated) {
      fetchStatus().then(setStatus).catch(() => {})
      fetchAppIdentity().then(setIdentity).catch(() => {})
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

  async function handleSaveIdentity(e: FormEvent) {
    e.preventDefault()
    setIdentitySaveState('saving')
    setIdentityError(null)
    try {
      const saved = await saveAppIdentity(password, identity)
      setIdentity(saved)
      setIdentitySaveState('saved')
      setTimeout(() => setIdentitySaveState('idle'), 3000)
    } catch (e: unknown) {
      setIdentitySaveState('error')
      setIdentityError(e instanceof Error ? e.message : 'Save failed')
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

        <section className="settings-section">
          <h2>App Identity</h2>
          <p className="settings-section-desc">
            These details appear in the Schema Explorer sidebar so users know who to contact about this deployment.
          </p>
          <form onSubmit={handleSaveIdentity} className="identity-form">
            <div className="identity-grid">
              <div className="identity-field">
                <label htmlFor="owner-name">Owner Name</label>
                <input
                  id="owner-name"
                  type="text"
                  placeholder="e.g. Jane Smith"
                  value={identity.owner_name}
                  onChange={e => setIdentity(prev => ({ ...prev, owner_name: e.target.value }))}
                />
              </div>
              <div className="identity-field">
                <label htmlFor="owner-title">Owner Title</label>
                <input
                  id="owner-title"
                  type="text"
                  placeholder="e.g. Data Engineer"
                  value={identity.owner_title}
                  onChange={e => setIdentity(prev => ({ ...prev, owner_title: e.target.value }))}
                />
              </div>
              <div className="identity-field">
                <label htmlFor="owner-email">Owner Email</label>
                <input
                  id="owner-email"
                  type="email"
                  placeholder="e.g. owner@company.com"
                  value={identity.owner_email}
                  onChange={e => setIdentity(prev => ({ ...prev, owner_email: e.target.value }))}
                />
              </div>
              <div className="identity-field">
                <label htmlFor="team-name">Team Name</label>
                <input
                  id="team-name"
                  type="text"
                  placeholder="e.g. Data Platform"
                  value={identity.team_name}
                  onChange={e => setIdentity(prev => ({ ...prev, team_name: e.target.value }))}
                />
              </div>
              <div className="identity-field identity-field-full">
                <label htmlFor="company-name">Company Name</label>
                <input
                  id="company-name"
                  type="text"
                  placeholder="e.g. Acme Corp"
                  value={identity.company_name}
                  onChange={e => setIdentity(prev => ({ ...prev, company_name: e.target.value }))}
                />
              </div>
            </div>

            <div className="identity-actions">
              <button
                type="submit"
                disabled={identitySaveState === 'saving'}
                className="save-identity-btn"
              >
                {identitySaveState === 'saving' ? 'Saving…' : 'Save Identity'}
              </button>
              {identitySaveState === 'saved' && (
                <p className="refresh-success" role="status" aria-live="polite">
                  ✓ Saved — sidebar footer will update immediately
                </p>
              )}
              {identitySaveState === 'error' && identityError && (
                <p className="refresh-error" role="alert" aria-live="assertive">
                  ✗ {identityError}
                </p>
              )}
            </div>
          </form>
        </section>
      </div>
    </div>
  )
}
