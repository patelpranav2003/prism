import { useState } from 'react'

interface Props {
  sql: string
}

export default function SQLViewer({ sql }: Props) {
  const [copied, setCopied] = useState(false)

  async function handleCopy() {
    await navigator.clipboard.writeText(sql)
    setCopied(true)
    setTimeout(() => setCopied(false), 2000)
  }

  return (
    <div className="sql-viewer">
      <div className="sql-viewer-header">
        <span className="sql-viewer-label">Generated SQL</span>
        <button
          onClick={handleCopy}
          className="copy-btn"
          aria-label="Copy SQL to clipboard"
          type="button"
        >
          {copied ? 'Copied!' : 'Copy'}
        </button>
      </div>
      <pre className="sql-code" aria-label="SQL query">
        <code>{sql}</code>
      </pre>
    </div>
  )
}
