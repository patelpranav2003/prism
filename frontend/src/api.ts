// API client — thin wrappers over fetch for all /api/* endpoints

import type {
  QueryResponse,
  SchemaModelDetail,
  SchemaModelSummary,
  StatusResponse,
} from './types'

const BASE = '/api'

async function _json<T>(res: Response): Promise<T> {
  if (!res.ok) {
    const body = await res.json().catch(() => ({}))
    throw new Error(body?.detail?.error ?? body?.error ?? `HTTP ${res.status}`)
  }
  return res.json() as Promise<T>
}

export async function fetchStatus(): Promise<StatusResponse> {
  return _json(await fetch(`${BASE}/status`))
}

export async function submitQuery(
  question: string,
  rowLimit = 1000,
): Promise<QueryResponse> {
  return _json(
    await fetch(`${BASE}/query`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ question, row_limit: rowLimit }),
    }),
  )
}

export async function fetchSchema(): Promise<SchemaModelSummary[]> {
  return _json(await fetch(`${BASE}/schema`))
}

export async function fetchSchemaModel(name: string): Promise<SchemaModelDetail> {
  return _json(await fetch(`${BASE}/schema/${encodeURIComponent(name)}`))
}

export async function triggerRefresh(password: string): Promise<{ success: boolean; model_count: number | null; error: string | null }> {
  return _json(
    await fetch(`${BASE}/refresh`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ password }),
    }),
  )
}

export async function authenticate(password: string): Promise<{ authenticated: boolean }> {
  return _json(
    await fetch(`${BASE}/auth`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ password }),
    }),
  )
}
