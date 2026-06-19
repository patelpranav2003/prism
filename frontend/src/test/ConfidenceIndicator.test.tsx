/**
 * Vitest unit tests for ConfidenceIndicator display mapping.
 *
 * Property 15: Confidence Indicator Display Mapping
 * For any confidence value from the set {"high", "medium", "low"}, the
 * ConfidenceIndicator component SHALL always render the label "High" (green),
 * "Medium" (amber), or "Low" (red) respectively — no other label or color
 * combination is valid.
 *
 * Validates: Requirements 7.4
 */

import { render, screen } from '@testing-library/react'
import { describe, expect, it } from 'vitest'
import ConfidenceIndicator from '../components/ConfidenceIndicator'
import type { Confidence } from '../types'

// --------------------------------------------------------------------------
// Property 15: Confidence Indicator Display Mapping
// Validates: Requirements 7.4
// --------------------------------------------------------------------------

describe('ConfidenceIndicator — Property 15', () => {
  const cases: { confidence: Confidence; expectedLabel: string; colorHint: string }[] = [
    { confidence: 'high', expectedLabel: 'High', colorHint: '#16a34a' },
    { confidence: 'medium', expectedLabel: 'Medium', colorHint: '#d97706' },
    { confidence: 'low', expectedLabel: 'Low', colorHint: '#dc2626' },
  ]

  it.each(cases)(
    'renders "$expectedLabel" for confidence="$confidence"',
    ({ confidence, expectedLabel }) => {
      render(<ConfidenceIndicator confidence={confidence} />)
      const el = screen.getByRole('status')
      expect(el).toHaveTextContent(expectedLabel)
    },
  )

  it.each(cases)(
    'has correct ARIA label for confidence="$confidence"',
    ({ confidence, expectedLabel }) => {
      render(<ConfidenceIndicator confidence={confidence} />)
      const el = screen.getByRole('status')
      expect(el).toHaveAttribute('aria-label', `Confidence: ${expectedLabel}`)
    },
  )

  it('renders only one of the three valid labels', () => {
    const validLabels = new Set(['High', 'Medium', 'Low'])
    for (const confidence of ['high', 'medium', 'low'] as Confidence[]) {
      const { unmount } = render(<ConfidenceIndicator confidence={confidence} />)
      const el = screen.getByRole('status')
      expect(validLabels).toContain(el.textContent)
      unmount()
    }
  })

  it('does not render any wrong label for "high"', () => {
    render(<ConfidenceIndicator confidence="high" />)
    expect(screen.queryByText('Low')).toBeNull()
    expect(screen.queryByText('Medium')).toBeNull()
  })

  it('does not render any wrong label for "low"', () => {
    render(<ConfidenceIndicator confidence="low" />)
    expect(screen.queryByText('High')).toBeNull()
    expect(screen.queryByText('Medium')).toBeNull()
  })
})
