import { useEffect, useRef, useState } from 'react'
import ReactMarkdown from 'react-markdown'
import './App.css'

type Message = {
  role: 'user' | 'assistant'
  content: string
}

// Quick access to the demo scenarios, so you don't have to remember the references
const DEMO_BOOKINGS = [
  { ref: 'AZX4K2', label: 'Marco · technical fault' },
  { ref: 'BRT9Q7', label: 'Sophie · storm delay' },
  { ref: 'KMW3P8', label: 'Lukas · crew strike, wheelchair' },
  { ref: 'GBX7T2', label: 'Giulia · cancelled 10 days ahead' },
  { ref: 'TMQ4L9', label: 'Thomas · cancelled 20 days ahead' },
  { ref: 'ACR5N8', label: 'Ana · ATC strike' },
  { ref: 'ECW2H6', label: 'Emily · long-haul delay' },
  { ref: 'PGB3K1', label: 'Paolo · bird strike' },
  { ref: 'LFD8V4', label: 'Luca · no disruption' },
]

function newSessionId(): string {
  return crypto.randomUUID()
}

export default function App() {
  const [sessionId, setSessionId] = useState(newSessionId)
  const [messages, setMessages] = useState<Message[]>([])
  const [input, setInput] = useState('')
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const bottomRef = useRef<HTMLDivElement>(null)

  // Keep the latest message in view
  useEffect(() => {
    bottomRef.current?.scrollIntoView({ behavior: 'smooth' })
  }, [messages, loading])

  async function send(text: string) {
    const trimmed = text.trim()
    if (!trimmed || loading) return

    setMessages((previous) => [...previous, { role: 'user', content: trimmed }])
    setInput('')
    setLoading(true)
    setError(null)

    try {
      const response = await fetch('/api/chat', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ message: trimmed, session_id: sessionId }),
      })
      if (!response.ok) {
        throw new Error(`The server answered with an error (${response.status}). Check the backend terminal.`)
      }
      const data: { reply: string } = await response.json()
      setMessages((previous) => [...previous, { role: 'assistant', content: data.reply }])
    } catch (e) {
      setError(e instanceof Error ? e.message : 'Something went wrong.')
    } finally {
      setLoading(false)
    }
  }

  function startNewConversation() {
    setSessionId(newSessionId())
    setMessages([])
    setInput('')
    setError(null)
  }

  return (
    <div className="app">
      <header className="header">
        <div>
          <h1>Aurora Airways · Disruption Assistant</h1>
          <p className="session">Session {sessionId.slice(0, 8)}</p>
        </div>
        <button className="secondary" onClick={startNewConversation} disabled={loading}>
          New conversation
        </button>
      </header>

      <div className="demo-bookings">
        <span>Demo bookings:</span>
        {DEMO_BOOKINGS.map((booking) => (
          <button
            key={booking.ref}
            className="chip"
            title={booking.label}
            onClick={() => setInput((current) => (current ? `${current} ${booking.ref}` : booking.ref))}
          >
            {booking.ref}
            <small>{booking.label}</small>
          </button>
        ))}
      </div>

      <main className="messages">
        {messages.length === 0 && (
          <p className="empty">Tell the assistant what happened to your flight to start.</p>
        )}

        {messages.map((message, index) => (
          <div key={index} className={`message ${message.role}`}>
            {message.role === 'assistant' ? (
              <ReactMarkdown>{message.content}</ReactMarkdown>
            ) : (
              <p>{message.content}</p>
            )}
          </div>
        ))}

        {loading && <div className="message assistant thinking">Thinking…</div>}
        {error && <div className="error">{error}</div>}
        <div ref={bottomRef} />
      </main>

      <form
        className="composer"
        onSubmit={(event) => {
          event.preventDefault()
          send(input)
        }}
      >
        <textarea
          value={input}
          onChange={(event) => setInput(event.target.value)}
          onKeyDown={(event) => {
            // Enter sends, Shift+Enter adds a new line
            if (event.key === 'Enter' && !event.shiftKey) {
              event.preventDefault()
              send(input)
            }
          }}
          placeholder="Type a message…"
          rows={2}
          disabled={loading}
        />
        <button type="submit" disabled={loading || !input.trim()}>
          Send
        </button>
      </form>
    </div>
  )
}