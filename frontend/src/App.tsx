import { useEffect, useRef, useState } from 'react'
import ReactMarkdown from 'react-markdown'
import './App.css'

// Same shapes as TurnStats, ToolUse and GuardHit in backend/models/chat.py
type ToolUse = {
  agent: string
  name: string
  ok: boolean
  duration_ms: number
}

type GuardHit = {
  agent: string
  name: string
  detail: string
}

type TurnStats = {
  duration_ms: number
  llm_calls: number
  input_tokens: number
  output_tokens: number
  cost_usd: number
  models: string[]
  fallbacks: number
  tools: ToolUse[]
  guards: GuardHit[]
}

type Message = {
  role: 'user' | 'assistant'
  content: string
  stats?: TurnStats | null
  traceUrl?: string | null
}

type ChatResponse = {
  reply: string
  session_id: string
  stats: TurnStats | null
  trace_url: string | null
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

function formatSeconds(ms: number): string {
  return `${(ms / 1000).toFixed(1)} s`
}

function formatCost(usd: number): string {
  if (usd === 0) return 'n/a'
  return usd < 0.01 ? `$${usd.toFixed(4)}` : `$${usd.toFixed(3)}`
}

// The "Details" dropdown under an assistant reply: what happened during that turn
function TurnDetails({ stats, traceUrl }: { stats: TurnStats; traceUrl?: string | null }) {
  const totalTokens = stats.input_tokens + stats.output_tokens
  const failedTools = stats.tools.filter((tool) => !tool.ok).length

  return (
    <details className="turn-details">
      <summary>
        {formatSeconds(stats.duration_ms)} · {stats.tools.length} tool calls · {totalTokens.toLocaleString()} tokens
        {stats.fallbacks > 0 && <span className="badge warning">fallback</span>}
        {stats.guards.length > 0 && (
          <span className="badge guard">
            {stats.guards.length} guard{stats.guards.length > 1 ? 's' : ''}
          </span>
        )}
        {failedTools > 0 && <span className="badge error">{failedTools} failed</span>}
      </summary>

      <div className="details-body">
        <dl className="details-grid">
          <dt>Time</dt>
          <dd>{formatSeconds(stats.duration_ms)}</dd>
          <dt>LLM calls</dt>
          <dd>{stats.llm_calls}</dd>
          <dt>Tokens</dt>
          <dd>
            {stats.input_tokens.toLocaleString()} in · {stats.output_tokens.toLocaleString()} out
          </dd>
          <dt>Cost</dt>
          <dd>{formatCost(stats.cost_usd)}</dd>
          <dt>Models</dt>
          <dd>
            {stats.models.join(', ') || '—'}
            {stats.fallbacks > 0 && ` (${stats.fallbacks} call${stats.fallbacks > 1 ? 's' : ''} fell back)`}
          </dd>
        </dl>

        {stats.tools.length > 0 && (
          <>
            <h4>Tools</h4>
            <ol className="tool-list">
              {stats.tools.map((tool, index) => (
                <li key={index} className={tool.ok ? '' : 'failed'}>
                  <span className={`agent agent-${tool.agent}`}>{tool.agent}</span>
                  <code>{tool.name}</code>
                  <span className="muted">{tool.duration_ms} ms</span>
                  {!tool.ok && <span className="badge error">error</span>}
                </li>
              ))}
            </ol>
          </>
        )}

        {stats.guards.length > 0 && (
          <>
            <h4>Guards that fired</h4>
            <ul className="guard-list">
              {stats.guards.map((guard, index) => (
                <li key={index}>
                  <code>{guard.name}</code>
                  <span className="muted">
                    {guard.agent}: {guard.detail}
                  </span>
                </li>
              ))}
            </ul>
          </>
        )}

        {traceUrl && (
          <a className="trace-link" href={traceUrl} target="_blank" rel="noreferrer">
            View full trace in Langfuse ↗
          </a>
        )}
      </div>
    </details>
  )
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
      const data: ChatResponse = await response.json()
      setMessages((previous) => [
        ...previous,
        { role: 'assistant', content: data.reply, stats: data.stats, traceUrl: data.trace_url },
      ])
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

        {messages.map((message, index) =>
          message.role === 'assistant' ? (
            <div key={index} className="assistant-turn">
              <div className="message assistant">
                <ReactMarkdown>{message.content}</ReactMarkdown>
              </div>
              {message.stats && <TurnDetails stats={message.stats} traceUrl={message.traceUrl} />}
            </div>
          ) : (
            <div key={index} className="message user">
              <p>{message.content}</p>
            </div>
          ),
        )}

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