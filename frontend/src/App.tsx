import { useEffect, useRef, useState } from 'react'
import ReactMarkdown from 'react-markdown'
import AgentMap from './AgentMap'
import type { ChatResponse, Message, TurnStats } from './types'
import './App.css'

// The demo bookings. Clicking one adds its reference to the message box; the passenger
// still writes their own message.
const DEMO_BOOKINGS = [
  { ref: 'AZX4K2', name: 'Marco', what: 'Cancelled: technical fault' },
  { ref: 'BRT9Q7', name: 'Sophie', what: 'Delayed 5 h: storms' },
  { ref: 'KMW3P8', name: 'Lukas', what: 'Cancelled: crew strike, wheelchair' },
  { ref: 'GBX7T2', name: 'Giulia', what: 'Cancelled 10 days ahead' },
  { ref: 'TMQ4L9', name: 'Thomas', what: 'Cancelled 20 days ahead' },
  { ref: 'ACR5N8', name: 'Ana', what: 'Cancelled: air traffic control strike' },
  { ref: 'ECW2H6', name: 'Emily', what: 'Delayed 4 h: long-haul' },
  { ref: 'PGB3K1', name: 'Paolo', what: 'Cancelled: bird strike' },
  { ref: 'LFD8V4', name: 'Luca', what: 'No disruption' },
]

// The sub-agents: they are called like tools, but they are AI agents (LLMs) themselves
const AGENT_TOOLS = new Set(['rebooking_agent', 'compensation_agent'])

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
function TurnDetails({
  stats,
  traceUrl,
  onReplay,
}: {
  stats: TurnStats
  traceUrl?: string | null
  onReplay: () => void
}) {
  const totalTokens = stats.input_tokens + stats.output_tokens
  const failedTools = stats.tools.filter((tool) => !tool.ok).length

  return (
    <details className="turn-details">
      <summary>
        Details: {formatSeconds(stats.duration_ms)}, {stats.tools.length} tool calls, {totalTokens.toLocaleString()} tokens
        {stats.fallbacks > 0 && <span className="badge badge-warning">fallback model</span>}
        {stats.guards.length > 0 && (
          <span className="badge badge-guard">
            {stats.guards.length} safety check{stats.guards.length > 1 ? 's' : ''}
          </span>
        )}
        {failedTools > 0 && <span className="badge badge-error">{failedTools} refused</span>}
      </summary>

      <div className="details-body">
        <dl className="details-grid">
          <dt>Time</dt>
          <dd>{formatSeconds(stats.duration_ms)}</dd>
          <dt>Model calls</dt>
          <dd>{stats.llm_calls}</dd>
          <dt>Tokens</dt>
          <dd>
            {stats.input_tokens.toLocaleString()} in, {stats.output_tokens.toLocaleString()} out
          </dd>
          <dt>Cost</dt>
          <dd>{formatCost(stats.cost_usd)} at paid prices</dd>
          <dt>Models</dt>
          <dd>
            {stats.models.join(', ') || 'none'}
            {stats.fallbacks > 0 && ` (${stats.fallbacks} call${stats.fallbacks > 1 ? 's' : ''} fell back)`}
          </dd>
        </dl>

        {stats.tools.length > 0 && (
          <>
            <h4>Tools and agents called, in order</h4>
            <ol className="tool-list">
              {stats.tools.map((tool, index) => (
                <li key={index} className={tool.ok ? '' : 'is-failed'}>
                  <code>{tool.name}</code>
                  <span className={`kind kind-${AGENT_TOOLS.has(tool.name) ? 'agent' : 'tool'}`}>
                    {AGENT_TOOLS.has(tool.name) ? 'AI agent' : 'Tool'}
                  </span>
                  <span className="muted">
                    called by {tool.agent === 'supervisor' ? 'the supervisor' : `the ${tool.agent} agent`},{' '}
                    {tool.duration_ms} ms{tool.ok ? '' : ', refused or failed'}
                  </span>
                </li>
              ))}
            </ol>
          </>
        )}

        {stats.guards.length > 0 && (
          <>
            <h4>Safety checks</h4>
            <ul className="guard-list">
              {stats.guards.map((guard, index) => (
                <li key={index}>
                  <code>{guard.name}</code> <span className="muted">{guard.detail}</span>
                </li>
              ))}
            </ul>
          </>
        )}

        <div className="details-actions">
          {stats.tools.length > 0 && (
            <button type="button" className="link-button" onClick={onReplay}>
              Replay on the map
            </button>
          )}
          {traceUrl && (
            <a href={traceUrl} target="_blank" rel="noreferrer">
              Open the full trace in Langfuse
            </a>
          )}
        </div>
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
  // The turn shown on the map, and a counter that restarts its replay
  const [mapStats, setMapStats] = useState<TurnStats | null>(null)
  const [replayKey, setReplayKey] = useState(0)
  // On small screens the map is hidden behind a toggle
  const [mapOpen, setMapOpen] = useState(false)
  const bottomRef = useRef<HTMLDivElement>(null)
  const inputRef = useRef<HTMLTextAreaElement>(null)

  // Keep the latest message in view (not on the welcome screen, which starts at the top)
  useEffect(() => {
    if (messages.length === 0 && !loading) return
    bottomRef.current?.scrollIntoView({ behavior: 'smooth' })
  }, [messages, loading])

  function showOnMap(stats: TurnStats) {
    setMapStats(stats)
    setReplayKey((key) => key + 1)
  }

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
      if (data.stats) showOnMap(data.stats)
    } catch (e) {
      setError(e instanceof Error ? e.message : 'Something went wrong.')
    } finally {
      setLoading(false)
    }
  }

  // Add a demo booking reference to whatever the passenger is typing, and put the cursor back
  function addReference(ref: string) {
    setInput((current) => (current.trim() ? `${current.trimEnd()} ${ref}` : ref))
    inputRef.current?.focus()
  }

  function startNewConversation() {
    setSessionId(newSessionId())
    setMessages([])
    setInput('')
    setError(null)
    setMapStats(null)
    setReplayKey((key) => key + 1)
  }

  return (
    <div className="app">
      <header className="header">
        <div>
          <h1>Aurora Airways disruption assistant</h1>
          <p className="tagline">
            A supervisor agent working with a rebooking agent and an EU261 compensation agent. Fictional airline,
            demo data.
          </p>
        </div>
        <button className="button-secondary" onClick={startNewConversation} disabled={loading}>
          New conversation
        </button>
      </header>

      <div className="layout">
        <section className="chat" aria-label="Conversation">
          <div className="demo-bookings">
            <p className="demo-bookings-hint">Demo bookings: click one to add its reference to your message.</p>
            <div className="demo-bookings-list">
              {DEMO_BOOKINGS.map((booking) => (
                <button
                  key={booking.ref}
                  type="button"
                  className="booking-chip"
                  onClick={() => addReference(booking.ref)}
                  disabled={loading}
                >
                  <span className="booking-chip-top">
                    <span className="booking-chip-name">{booking.name}</span>
                    <code>{booking.ref}</code>
                  </span>
                  <span className="booking-chip-what">{booking.what}</span>
                </button>
              ))}
            </div>
          </div>

          <main className="messages">
            {messages.length === 0 && (
              <div className="welcome">
                <h2>Tell the assistant what happened to your flight</h2>
                <p>
                  For example: "My flight has been cancelled, my booking reference is AZX4K2." Use one of the demo
                  bookings above, or any reference you like. Under each reply, open Details to see the tools, tokens
                  and safety checks behind it.
                </p>
              </div>
            )}

            {messages.map((message, index) =>
              message.role === 'assistant' ? (
                <div key={index} className="assistant-turn">
                  <div className="message message-assistant">
                    <ReactMarkdown>{message.content}</ReactMarkdown>
                  </div>
                  {message.stats && (
                    <TurnDetails
                      stats={message.stats}
                      traceUrl={message.traceUrl}
                      onReplay={() => {
                        showOnMap(message.stats!)
                        setMapOpen(true)
                      }}
                    />
                  )}
                </div>
              ) : (
                <div key={index} className="message message-user">
                  <p>{message.content}</p>
                </div>
              ),
            )}

            {loading && <div className="message message-assistant thinking">Working on it…</div>}
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
              ref={inputRef}
              value={input}
              onChange={(event) => setInput(event.target.value)}
              onKeyDown={(event) => {
                // Enter sends, Shift+Enter adds a new line
                if (event.key === 'Enter' && !event.shiftKey) {
                  event.preventDefault()
                  send(input)
                }
              }}
              placeholder="Type a message"
              aria-label="Your message"
              rows={2}
              disabled={loading}
            />
            <button type="submit" disabled={loading || !input.trim()}>
              Send
            </button>
          </form>
        </section>

        <aside className={`map-panel ${mapOpen ? 'is-open' : ''}`}>
          <button
            type="button"
            className="map-toggle"
            aria-expanded={mapOpen}
            onClick={() => setMapOpen((open) => !open)}
          >
            {mapOpen ? 'Hide how it worked' : 'Show how it worked'}
          </button>
          <div className="map-body">
            <h2 className="map-title">How the last reply was made</h2>
            <AgentMap stats={mapStats} replayKey={replayKey} working={loading} />
          </div>
        </aside>
      </div>
    </div>
  )
}