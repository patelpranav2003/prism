import { useState, type FormEvent } from 'react'
import type { CacheStatus } from '../types'

interface Props {
  onSubmit: (question: string) => void
  loading: boolean
  cacheStatus: CacheStatus | null
  suggestedQuestions?: string[]
}

export default function SearchBar({ onSubmit, loading, cacheStatus, suggestedQuestions = [] }: Props) {
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

      {cacheStatus !== 'unavailable' && suggestedQuestions.length > 0 && (
        <div className="example-chips" role="list" aria-label="Example questions">
          {suggestedQuestions.map(q => (
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
