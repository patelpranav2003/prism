import type { CacheStatus, StatusResponse } from '../types'

interface Props {
  status: StatusResponse | null
}

function formatElapsed(utcString: string): string {
  const then = new Date(utcString).getTime()
  const now = Date.now()
  const diffMin = Math.floor((now - then) / 60000)
  if (diffMin < 1) return 'just now'
  if (diffMin < 60) return `${diffMin}m ago`
  const diffHr = Math.floor(diffMin / 60)
  return `${diffHr}h ago`
}

export default function SchemaHealthBar({ status }: Props) {
  if (!status) {
    return (
      <div className="schema-health-bar schema-health-loading" aria-live="polite">
        Loading schema…
      </div>
    )
  }

  const { cache_status, model_count, last_refresh_utc } = status

  if (cache_status === 'unavailable') {
    return (
      <div
        className="schema-health-bar schema-health-unavailable"
        role="alert"
        aria-live="assertive"
        style={{ color: '#dc2626', fontWeight: 600 }}
      >
        Schema unavailable — contact your data team
      </div>
    )
  }

  const elapsed = last_refresh_utc ? formatElapsed(last_refresh_utc) : 'unknown'

  if (cache_status === 'stale') {
    return (
      <div
        className="schema-health-bar schema-health-stale"
        role="status"
        style={{ color: '#d97706' }}
      >
        {model_count} models · last refreshed {elapsed} · ⚠ schema may be stale
      </div>
    )
  }

  // fresh
  return (
    <div className="schema-health-bar schema-health-fresh" role="status" style={{ color: '#16a34a' }}>
      {model_count} models · last refreshed {elapsed}
    </div>
  )
}
