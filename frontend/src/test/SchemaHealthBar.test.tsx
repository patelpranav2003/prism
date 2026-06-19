/**
 * Vitest unit tests for SchemaHealthBar display mapping.
 *
 * Property 16: Schema Health Indicator Display Mapping
 * For any CacheStatus value from the set {"fresh", "stale", "unavailable"},
 * the SchemaHealthBar component SHALL always display the correct text:
 * model count + elapsed time for "fresh"; model count + warning label for
 * "stale"; and "Schema unavailable — contact your data team" for "unavailable".
 *
 * Validates: Requirements 8.3
 */

import { render, screen } from '@testing-library/react'
import { describe, expect, it } from 'vitest'
import SchemaHealthBar from '../components/SchemaHealthBar'
import type { StatusResponse } from '../types'

function makeStatus(
  cache_status: StatusResponse['cache_status'],
  model_count = 42,
  last_refresh_utc: string | null = new Date(Date.now() - 60000).toISOString(),
): StatusResponse {
  return { cache_status, model_count, last_refresh_utc }
}

// --------------------------------------------------------------------------
// Property 16: Schema Health Indicator Display Mapping
// Validates: Requirements 8.3
// --------------------------------------------------------------------------

describe('SchemaHealthBar — Property 16', () => {
  it('renders unavailability message for status="unavailable"', () => {
    render(<SchemaHealthBar status={makeStatus('unavailable')} />)
    expect(screen.getByRole('alert')).toHaveTextContent(
      'Schema unavailable — contact your data team',
    )
  })

  it('shows model count for status="fresh"', () => {
    render(<SchemaHealthBar status={makeStatus('fresh', 57)} />)
    const el = screen.getByRole('status')
    expect(el).toHaveTextContent('57')
  })

  it('shows model count for status="stale"', () => {
    render(<SchemaHealthBar status={makeStatus('stale', 33)} />)
    const el = screen.getByRole('status')
    expect(el).toHaveTextContent('33')
  })

  it('shows stale warning label for status="stale"', () => {
    render(<SchemaHealthBar status={makeStatus('stale')} />)
    const el = screen.getByRole('status')
    expect(el.textContent?.toLowerCase()).toContain('stale')
  })

  it('does NOT show unavailability message for status="fresh"', () => {
    render(<SchemaHealthBar status={makeStatus('fresh')} />)
    expect(screen.queryByText(/Schema unavailable/)).toBeNull()
  })

  it('does NOT show unavailability message for status="stale"', () => {
    render(<SchemaHealthBar status={makeStatus('stale')} />)
    expect(screen.queryByText(/Schema unavailable/)).toBeNull()
  })

  it('renders loading state when status is null', () => {
    render(<SchemaHealthBar status={null} />)
    expect(document.body).toHaveTextContent(/loading/i)
  })

  it('renders all three statuses without throwing', () => {
    for (const s of ['fresh', 'stale', 'unavailable'] as const) {
      const { unmount } = render(<SchemaHealthBar status={makeStatus(s)} />)
      unmount()
    }
  })
})
