import { useEffect, useRef, useState, type KeyboardEvent, type FormEvent } from 'react'
import type { ChatEntry, ConversationMessage, QueryResponse, SchemaModelSummary, StatusResponse } from '../types'
import { fetchSchema, fetchStatus, submitQuery } from '../api'
import SchemaExplorer from '../components/SchemaExplorer'
import SchemaHealthBar from '../components/SchemaHealthBar'
import AssistantMessage from '../components/AssistantMessage'
import UserMessage from '../components/UserMessage'

let _id = 0
function nextId() { return String(++_id) }

function generateSuggestedQuestions(models: SchemaModelSummary[]): string[] {
  const gold = models.filter(m => m.layer === 'gold')
  if (gold.length === 0) return []

  // Group by domain (prefix before _gold_) so chips span different areas
  const byDomain = new Map<string, SchemaModelSummary[]>()
  for (const m of gold) {
    const idx = m.name.indexOf('_gold_')
    const domain = idx !== -1 ? m.name.slice(0, idx) : 'other'
    if (!byDomain.has(domain)) byDomain.set(domain, [])
    byDomain.get(domain)!.push(m)
  }

  // Shuffle domains so suggestions vary each page load
  const domains = Array.from(byDomain.entries())
  for (let i = domains.length - 1; i > 0; i--) {
    const j = Math.floor(Math.random() * (i + 1))
    ;[domains[i], domains[j]] = [domains[j], domains[i]]
  }

  const grainTemplates: Record<string, (d: string, t: string) => string> = {
    day:   (d, t) => `What are the daily ${t} trends for ${d}?`,
    week:  (d, t) => `Show me weekly ${t} for ${d}`,
    month: (d, t) => `How has ${d} ${t} changed month over month?`,
    '':    (d, t) => `Explore ${d} ${t} data`,
  }

  const questions: string[] = []
  for (const [, domainModels] of domains) {
    if (questions.length >= 5) break
    const m = domainModels[Math.floor(Math.random() * domainModels.length)]
    const goldIdx = m.name.indexOf('_gold_')
    if (goldIdx === -1) continue
    const domain = m.name.slice(0, goldIdx).replace(/_/g, ' ').replace(/\b\w/g, c => c.toUpperCase())
    const rest = m.name.slice(goldIdx + 6)
    const grainSep = rest.indexOf('__')
    const topic = (grainSep !== -1 ? rest.slice(0, grainSep) : rest).replace(/_/g, ' ')
    const grain = grainSep !== -1 ? rest.slice(grainSep + 2) : ''
    questions.push((grainTemplates[grain] ?? grainTemplates[''])(domain, topic))
  }
  return questions
}

export default function Chat() {
  const [messages, setMessages] = useState<ChatEntry[]>([])
  const [input, setInput] = useState('')
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [status, setStatus] = useState<StatusResponse | null>(null)
  const [models, setModels] = useState<SchemaModelSummary[]>([])
  const [suggestedQuestions, setSuggestedQuestions] = useState<string[]>([])
  const [highlightModel, setHighlightModel] = useState<string | null>(null)
  const [showExplorer, setShowExplorer] = useState(false)
  const [sidebarWidth, setSidebarWidth] = useState(300)
  const threadRef = useRef<HTMLDivElement>(null)
  const textareaRef = useRef<HTMLTextAreaElement>(null)
  const sidebarDragging = useRef(false)

  function handleSidebarResizeMouseDown(e: React.MouseEvent) {
    e.preventDefault()
    sidebarDragging.current = true
    const startX = e.clientX
    const startWidth = sidebarWidth
    function onMove(ev: MouseEvent) {
      if (!sidebarDragging.current) return
      setSidebarWidth(Math.max(220, Math.min(640, startWidth + ev.clientX - startX)))
    }
    function onUp() {
      sidebarDragging.current = false
      window.removeEventListener('mousemove', onMove)
      window.removeEventListener('mouseup', onUp)
    }
    window.addEventListener('mousemove', onMove)
    window.addEventListener('mouseup', onUp)
  }

  useEffect(() => {
    fetchStatus().then(setStatus).catch(() => {})
    fetchSchema().then(ms => { setModels(ms); setSuggestedQuestions(generateSuggestedQuestions(ms)) }).catch(() => {})
    const interval = setInterval(() => fetchStatus().then(setStatus).catch(() => {}), 5000)
    return () => clearInterval(interval)
  }, [])

  useEffect(() => {
    if (threadRef.current) {
      threadRef.current.scrollTop = threadRef.current.scrollHeight
    }
  }, [messages, loading])

  function buildHistory(current: ChatEntry[]): ConversationMessage[] {
    const hist: ConversationMessage[] = []
    for (const msg of current) {
      if (msg.type === 'user' && msg.question) {
        hist.push({ role: 'user', content: msg.question })
      } else if (msg.type === 'assistant' && msg.result) {
        hist.push({ role: 'assistant', content: msg.result.sql_result.explanation })
      }
    }
    return hist
  }

  async function handleSubmit(question: string) {
    const q = question.trim()
    if (!q || loading) return
    setInput('')
    setError(null)
    if (textareaRef.current) {
      textareaRef.current.style.height = 'auto'
    }

    // Capture history BEFORE adding the new user message (so it only contains completed exchanges)
    const history = buildHistory(messages)

    setMessages(prev => [...prev, { id: nextId(), type: 'user', question: q }])
    setLoading(true)

    try {
      const result: QueryResponse = await submitQuery(q, history)
      setMessages(prev => [...prev, { id: nextId(), type: 'assistant', result }])
    } catch (e: unknown) {
      setError(e instanceof Error ? e.message : 'Something went wrong. Please try again.')
    } finally {
      setLoading(false)
    }
  }

  function handleKeyDown(e: KeyboardEvent<HTMLTextAreaElement>) {
    if (e.key === 'Enter' && !e.shiftKey) {
      e.preventDefault()
      handleSubmit(input)
    }
  }

  function handleInput(e: FormEvent<HTMLTextAreaElement>) {
    const el = e.currentTarget
    el.style.height = 'auto'
    el.style.height = Math.min(el.scrollHeight, 130) + 'px'
  }

  const cacheUnavailable = status?.cache_status === 'unavailable'
  const disabled = loading || cacheUnavailable || status === null
  const hasMessages = messages.length > 0

  return (
    <div className="chat-page">
      {showExplorer && (
        <>
          <div className="chat-sidebar-wrapper" style={{ width: sidebarWidth }}>
            <SchemaExplorer
              models={models}
              highlightModel={highlightModel}
              onClose={() => setShowExplorer(false)}
            />
          </div>
          <div className="sidebar-resize-handle" onMouseDown={handleSidebarResizeMouseDown} title="Drag to resize" />
        </>
      )}

      <div className="chat-main">
        <header className="chat-header">
          <div className="prism-brand-header">
            <svg width="28" height="28" viewBox="0 0 20 20" fill="none" aria-hidden="true">
              <defs>
                <linearGradient id="prism-grad-hdr" x1="0" y1="0" x2="1" y2="1">
                  <stop offset="0%" stopColor="#818cf8"/>
                  <stop offset="100%" stopColor="#1d4ed8"/>
                </linearGradient>
              </defs>
              <polygon points="10,1 19,18 1,18" fill="url(#prism-grad-hdr)"/>
              <line x1="10" y1="1" x2="10" y2="18" stroke="white" strokeWidth="0.8" strokeOpacity="0.35"/>
            </svg>
            <span className="prism-brand-text">Prism</span>
          </div>
          <div className="chat-header-right">
            <SchemaHealthBar status={status} />
            {hasMessages && (
              <button
                type="button"
                className="icon-btn"
                onClick={() => { setMessages([]); setError(null) }}
                aria-label="Start a new conversation"
              >
                + New chat
              </button>
            )}
            <button
              type="button"
              className="icon-btn"
              onClick={() => setShowExplorer(s => !s)}
              aria-label={showExplorer ? 'Hide schema explorer' : 'Show schema explorer'}
            >
              {showExplorer ? 'Hide Schema' : 'Schema'}
            </button>
            <a href="/admin" className="icon-btn" aria-label="Settings">Settings</a>
          </div>
        </header>

        <div className="chat-thread" ref={threadRef} aria-live="polite" aria-label="Conversation">
          {!hasMessages ? (
            <div className="chat-welcome">
              <div className="chat-welcome-brand">
                <svg width="72" height="72" viewBox="0 0 20 20" fill="none" aria-hidden="true">
                  <defs>
                    <linearGradient id="prism-grad-welcome" x1="0" y1="0" x2="1" y2="1">
                      <stop offset="0%" stopColor="#818cf8"/>
                      <stop offset="100%" stopColor="#1d4ed8"/>
                    </linearGradient>
                  </defs>
                  <polygon points="10,1 19,18 1,18" fill="url(#prism-grad-welcome)"/>
                  <line x1="10" y1="1" x2="10" y2="18" stroke="white" strokeWidth="0.8" strokeOpacity="0.4"/>
                </svg>
                <span className="prism-welcome-text">Prism</span>
              </div>
              <div>
                <p className="chat-welcome-title">What would you like to know?</p>
                <p className="chat-welcome-subtitle">Ask anything about your data in plain English</p>
              </div>
              {suggestedQuestions.length > 0 && (
                <div className="chat-welcome-chips">
                  <div className="example-chips" role="list">
                    {suggestedQuestions.map(q => (
                      <button
                        key={q}
                        type="button"
                        role="listitem"
                        className="chip-btn"
                        disabled={disabled}
                        onClick={() => handleSubmit(q)}
                        aria-label={`Ask: ${q}`}
                      >
                        {q}
                      </button>
                    ))}
                  </div>
                </div>
              )}

            </div>
          ) : (
            <div style={{ display: 'flex', flexDirection: 'column' }}>
              {messages.map(msg =>
                msg.type === 'user' ? (
                  <div key={msg.id} className="message-row user">
                    <UserMessage question={msg.question!} />
                  </div>
                ) : (
                  <div key={msg.id} className="message-row assistant">
                    <div className="assistant-avatar" aria-hidden="true">P</div>
                    <AssistantMessage
                      result={msg.result!}
                      onModelClick={name => { setHighlightModel(name); setShowExplorer(true) }}
                    />
                  </div>
                )
              )}

              {loading && (
                <div className="thinking-row">
                  <div className="assistant-avatar" aria-hidden="true">P</div>
                  <div className="thinking-dots" aria-label="Prism is thinking">
                    <div className="dot" />
                    <div className="dot" />
                    <div className="dot" />
                  </div>
                </div>
              )}

              {error && (
                <div className="chat-error-row">
                  <div className="chat-error-msg" role="alert">{error}</div>
                </div>
              )}
            </div>
          )}
        </div>

        <div className="chat-input-bar">
          <div className="chat-input-inner">
            <textarea
              ref={textareaRef}
              value={input}
              onChange={e => setInput(e.target.value)}
              onKeyDown={handleKeyDown}
              onInput={handleInput}
              placeholder={
                cacheUnavailable
                  ? 'Schema unavailable — cannot process queries'
                  : 'Ask anything about your data… (Enter to send, Shift+Enter for new line)'
              }
              disabled={disabled}
              className="chat-textarea"
              rows={1}
              aria-label="Ask a data question"
            />
            <button
              type="button"
              onClick={() => handleSubmit(input)}
              disabled={disabled || !input.trim()}
              className="chat-send-btn"
              aria-label="Send"
            >
              {loading ? '…' : 'Send'}
            </button>
          </div>
        </div>

        {(status?.owner_name || status?.company_name || status?.team_name) && (
          <div className="chat-page-identity">
            {[
              status.owner_name,
              status.owner_title,
              status.owner_email
                ? { email: status.owner_email }
                : null,
              status.team_name,
              status.company_name,
            ]
              .filter(Boolean)
              .map((item, i, arr) =>
                typeof item === 'object' && item !== null && 'email' in item ? (
                  <span key={i}>
                    <a href={`mailto:${item.email}`} className="identity-email-link">{item.email}</a>
                    {i < arr.length - 1 && <span className="identity-sep">·</span>}
                  </span>
                ) : (
                  <span key={i}>
                    {item as string}
                    {i < arr.length - 1 && <span className="identity-sep">·</span>}
                  </span>
                )
              )}
          </div>
        )}
      </div>
    </div>
  )
}
