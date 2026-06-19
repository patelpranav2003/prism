import type { Confidence } from '../types'

interface Props {
  confidence: Confidence
}

const CONFIG: Record<Confidence, { label: string; color: string }> = {
  high: { label: 'High', color: '#16a34a' },    // green-600
  medium: { label: 'Medium', color: '#d97706' }, // amber-600
  low: { label: 'Low', color: '#dc2626' },       // red-600
}

export default function ConfidenceIndicator({ confidence }: Props) {
  const { label, color } = CONFIG[confidence]
  return (
    <span
      aria-label={`Confidence: ${label}`}
      role="status"
      style={{
        display: 'inline-block',
        padding: '2px 10px',
        borderRadius: '12px',
        fontSize: '0.8rem',
        fontWeight: 600,
        backgroundColor: color + '22',
        color,
        border: `1px solid ${color}`,
      }}
    >
      {label}
    </span>
  )
}
