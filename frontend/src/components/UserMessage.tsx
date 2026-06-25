interface Props {
  question: string
}

export default function UserMessage({ question }: Props) {
  return <div className="user-bubble">{question}</div>
}
