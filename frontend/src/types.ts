// Shared TypeScript types matching backend Pydantic models

export type CacheStatus = 'fresh' | 'stale' | 'unavailable'
export type Confidence = 'high' | 'medium' | 'low'
export type Layer = 'gold' | 'silver' | 'bronze'

export interface ConversationMessage {
  role: 'user' | 'assistant'
  content: string
}

export interface ChatEntry {
  id: string
  type: 'user' | 'assistant'
  question?: string
  result?: QueryResponse
  error?: string
}

export interface StatusResponse {
  cache_status: CacheStatus
  last_refresh_utc: string | null
  model_count: number
  owner_name?: string | null
  owner_title?: string | null
  owner_email?: string | null
  team_name?: string | null
  company_name?: string | null
}

export interface SQLResultData {
  sql: string
  explanation: string
  models_used: string[]
  confidence: Confidence
  confidence_reason: string
}

export interface QueryResponse {
  sql_result: SQLResultData
  rows: Record<string, unknown>[]
  row_count: number
  execution_time_ms: number
  warehouse_name: string
  correlation_id: string
}

export interface AppIdentityData {
  owner_name: string
  owner_title: string
  owner_email: string
  team_name: string
  company_name: string
}

export interface SchemaModelSummary {
  name: string
  fqn: string
  layer: Layer
  description: string
  column_count: number
  row_count: number
  last_updated: string | null
}

export interface ColumnDetail {
  name: string
  data_type: string
  description: string
}

export interface SchemaModelDetail {
  name: string
  fqn: string
  layer: Layer
  description: string
  grain: string
  columns: ColumnDetail[]
  row_count: number
  last_updated: string | null
  depends_on: string[]
  tags: string[]
  compiled_sql_excerpt: string
  parents: string[]
  children: string[]
}
