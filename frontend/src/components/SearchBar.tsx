import { useState, type FormEvent } from 'react'
import type { CacheStatus } from '../types'

const EXAMPLE_QUESTIONS = [
  'What is total revenue by region last quarter?',
  'Show me the top 10 customers by order count',
  'How many new users signed up each month this year?',
  'What is the average order value by product category?',
  'Which products have declining sales over the last 3 months?',
]

interface Props {
  onSubmit: (question: string) => void
  loading: boolean
  cacheStatus: CacheStatus | null
}

export default function SearchBar({ onSubmit, loading, cacheStatus }: Props) {
  const [question, setQuestion] = useState('')
  const disabled = loading || cacheStatus === 'unavailable' || cacheStatus === null

  function handleSubmit(e: FormEvent) {
    e.preventDefault()
    const q = question.trim()
    if (q && !disabled) onSubmit(q)
  }

  function handleChipClick(q: string) {
    if (disabled) return
    setQuestion(q)
    onSubmit(q)
  }

  return (
    <div className="search-bar-container">
      <form onSubmit={handleSubmit} role="search">
        <div className="search-input-row">
          <input
            type="text"
            value={question}
            onChange={e => setQuestion(e.target.value)}
            placeholder="Ask anything about your data…"
            disabled={disabled}
            aria-label="Ask a data question"
            className="search-input"
            aria-disabled={disabled}
          />
          <button
            type="submit"
            disabled={disabled || !question.trim()}
            className="search-submit-btn"
            aria-label="Submit question"
          >
            {loading ? 'Thinking…' : 'Ask'}
          </button>
        </div>
      </form>

      {cacheStatus !== 'unavailable' && (
        <div className="example-chips" role="list" aria-label="Example questions">
          {EXAMPLE_QUESTIONS.map(q => (
            <button
              key={q}
              type="button"
              role="listitem"
              className="chip-btn"
              disabled={disabled}
              onClick={() => handleChipClick(q)}
              aria-label={`Ask: ${q}`}
            >
              {q}
            </button>
          ))}
        </div>
      )}
    </div>
  )
}
