import { useEffect, useState } from 'react'
import type { TurnStats } from './types'

// The route map: the supervisor is the hub, its tools are the stops, in the order of the 9 steps.
// Sub-agents have their own stops branching off them.
type Stop = {
  id: string            // the tool or agent name, exactly as in stats.tools
  what: string          // what it does, in plain words
  kind: 'tool' | 'agent'
  writes?: boolean      // writes to the database
  children?: Stop[]
}

const ROUTE: Stop[] = [
  { id: 'get_booking', what: 'Looks up the booking', kind: 'tool' },
  { id: 'get_disruption', what: 'Checks what happened to the flight', kind: 'tool' },
  {
    id: 'rebooking_agent',
    what: 'Finds and ranks new flights',
    kind: 'agent',
    children: [{ id: 'search_flights', what: 'Searches the flight inventory', kind: 'tool' }],
  },
  { id: 'record_rebooking', what: 'Books the new flight', kind: 'tool', writes: true },
  { id: 'compute_care_entitlements', what: 'Works out meals, hotel and transport', kind: 'tool' },
  {
    id: 'compensation_agent',
    what: 'Decides if compensation is owed under EU261',
    kind: 'agent',
    children: [
      { id: 'search_regulations', what: 'Searches EU261 case law', kind: 'tool' },
      { id: 'calculate_compensation', what: 'Calculates the amount', kind: 'tool' },
    ],
  },
  { id: 'close_case', what: 'Closes the case', kind: 'tool' },
]

// What each box does, shown when it is clicked (same style as the Details panel)
const DESCRIPTIONS: Record<string, string> = {
  supervisor:
    'The only one that talks to the passenger. It reads the conversation and the case file, decides which tool or agent to call next, and writes every reply. Code checks its tool calls and its replies.',
  get_booking:
    "Finds the booking in the airline's records: passenger, flight, route, times and special needs. It starts the case file for that booking.",
  get_disruption:
    'Checks whether the flight is cancelled or delayed, and why. Code adds the local departure and arrival times, so the model never calculates them.',
  rebooking_agent:
    "A separate LLM with its own instructions. It turns the passenger's wishes into flight searches and ranks what it finds. Code rebuilds every option from the real search results, so it can't invent a flight.",
  search_flights:
    'Searches the flight inventory with fixed, safe queries: only future flights with free seats, on the right route.',
  record_rebooking:
    'Books the chosen flight in the database. Code only allows a flight that was actually offered, and only after the passenger confirmed that exact flight.',
  compute_care_entitlements:
    'Works out meals, hotel nights and transport from the EU261 rules and the length of the wait. Pure code, no AI.',
  compensation_agent:
    "A separate LLM that reads EU261 case law and decides one thing: was the cause outside the airline's control? The amount is then calculated by code.",
  search_regulations:
    'Searches a small library of EU261 rules and court rulings by meaning, and returns the most relevant passages.',
  calculate_compensation:
    "Turns the agent's decision into an amount between €0 and €600, based on distance, notice and delay. Pure code, unit-tested.",
  close_case:
    'Checks that every step is done (flight, care, compensation) and returns the final summary.',
}

// Agents, for the "what is happening now" line
const AGENT_NAMES = new Set(['rebooking_agent', 'compensation_agent'])

function KindLabel({ kind }: { kind: 'tool' | 'agent' }) {
  return <span className={`kind kind-${kind}`}>{kind === 'agent' ? 'AI agent' : 'Tool'}</span>
}

// Plain-language names for the guards, for people who haven't read the code
const GUARD_LABELS: Record<string, string> = {
  booking_not_confirmed: 'Booking held until the passenger confirmed that exact flight',
  flight_not_offered: 'Refused to book a flight that was never offered',
  booking_ref_auto_filled: 'Filled in a missing booking reference',
  missing_arguments: 'Sent a tool call back for missing details',
  invented_flight_in_reply: 'Caught a reply mentioning a flight that does not exist',
  invented_flight_dropped: 'Dropped a flight the rebooking agent made up',
  invalid_answer_format: 'Sent a badly formatted answer back to be redone',
  empty_reply: 'Asked the model to write a reply it left empty',
  booking_from_case_file: 'Reused a booking already looked up',
  all_models_failed: 'Every model was unavailable for one step',
  tool_not_available: 'Model asked for a tool it does not have',
  unknown_tool: 'Model asked for a tool it does not have',
  unknown_booking: 'Model used a booking reference with no case',
  invalid_arguments: 'Model sent unreadable tool arguments',
  max_rounds: 'Stopped a turn that went on too long',
}

type Status = 'idle' | 'active' | 'done' | 'blocked' | 'failed'

type Props = {
  stats: TurnStats | null   // the turn to show (usually the latest reply)
  replayKey: number         // changes every time a replay should start
  working: boolean          // the assistant is thinking right now
}

function prefersReducedMotion(): boolean {
  return window.matchMedia('(prefers-reduced-motion: reduce)').matches
}

export default function AgentMap({ stats, replayKey, working }: Props) {
  const calls = stats?.tools ?? []
  // How many of the turn's tool calls have been "played" so far
  const [played, setPlayed] = useState(calls.length)
  // The box whose description is open (only one at a time)
  const [openId, setOpenId] = useState<string | null>(null)

  function toggle(id: string) {
    setOpenId((current) => (current === id ? null : id))
  }

  // Replay the turn: light the stops one after another, in the order the tools ran
  useEffect(() => {
    if (prefersReducedMotion() || calls.length === 0) {
      setPlayed(calls.length)
      return
    }
    setPlayed(0)
    const timer = window.setInterval(() => {
      setPlayed((current) => {
        if (current >= calls.length) {
          window.clearInterval(timer)
          return current
        }
        return current + 1
      })
    }, 450)
    return () => window.clearInterval(timer)
    // replayKey is what starts a new replay; calls belong to the same turn
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [replayKey])

  // The status of one stop, given the calls played so far
  function statusOf(id: string): Status {
    const seen = calls.slice(0, played).filter((call) => call.name === id)
    if (seen.length === 0) return 'idle'
    const isLatest = played < calls.length && calls[played - 1]?.name === id
    if (isLatest) return 'active'
    if (seen.some((call) => call.ok)) return 'done'
    // Held back by a safety check on purpose: not an error
    if (seen.some((call) => call.blocked)) return 'blocked'
    return 'failed'
  }

  const replaying = played < calls.length
  const current = replaying && played > 0 ? calls[played - 1].name : null
  const currentText = current
    ? AGENT_NAMES.has(current)
      ? `Handing over to ${current} (AI agent)`
      : `Calling the tool ${current}`
    : 'Replaying the last reply'
  const guards = stats?.guards ?? []

  function renderStop(stop: Stop, index: number | null) {
    const status = statusOf(stop.id)
    return (
      <li key={stop.id} className={`stop stop-${stop.kind} is-${status}`}>
        <span className="stop-marker" aria-hidden="true">
          {index !== null ? index : ''}
        </span>
        <button
          type="button"
          className="stop-button"
          aria-expanded={openId === stop.id}
          onClick={() => toggle(stop.id)}
        >
          <span className="stop-head">
            <code className="stop-name">{stop.id}</code>
            <KindLabel kind={stop.kind} />
          </span>
          <span className="stop-what">
            {stop.what}
            {stop.writes && <span className="stop-tag">, writes to the database</span>}
            {status === 'blocked' && <span className="stop-tag stop-tag-blocked"> (held by a safety check)</span>}
            {status === 'failed' && <span className="stop-tag stop-tag-failed"> (failed)</span>}
          </span>
        </button>
        {openId === stop.id && <p className="stop-description">{DESCRIPTIONS[stop.id]}</p>}
        {stop.children && <ol className="branch">{stop.children.map((child) => renderStop(child, null))}</ol>}
      </li>
    )
  }

  return (
    <section className="agent-map" aria-label="How the agents handled the last reply">
      <div className={`hub ${working ? 'is-working' : stats ? 'is-done' : ''}`}>
        <span className="hub-marker" aria-hidden="true" />
        <button
          type="button"
          className="stop-button"
          aria-expanded={openId === 'supervisor'}
          onClick={() => toggle('supervisor')}
        >
          <span className="stop-head">
            <code className="stop-name hub-name">supervisor</code>
            <KindLabel kind="agent" />
          </span>
          <span className="hub-status">
            {working
              ? 'Working on your message'
              : stats
                ? replaying
                  ? currentText
                  : `${calls.length} tool call${calls.length === 1 ? '' : 's'}, ${stats.llm_calls} model call${stats.llm_calls === 1 ? '' : 's'}`
                : 'Talks to the passenger and decides which tool or agent to call'}
          </span>
        </button>
      </div>
      {openId === 'supervisor' && <p className="stop-description hub-description">{DESCRIPTIONS.supervisor}</p>}

      <p className="map-hint">Click any box to see what it does.</p>

      <ol className="route">{ROUTE.map((stop, i) => renderStop(stop, i + 1))}</ol>

      {guards.length > 0 && !replaying && (
        <div className="checks">
          <h3>Safety checks that stepped in</h3>
          <ul>
            {guards.map((guard, i) => (
              <li key={i}>{GUARD_LABELS[guard.name] ?? guard.name}</li>
            ))}
          </ul>
        </div>
      )}

      <p className="map-legend">
        <span className="legend-item"><span className="legend-dot is-agent" /> AI agent (an LLM)</span>
        <span className="legend-item"><span className="legend-dot" /> Tool (plain code)</span>
        <span className="legend-item"><span className="legend-dot is-done" /> used</span>
        <span className="legend-item"><span className="legend-dot is-blocked" /> held by a safety check</span>
        <span className="legend-item"><span className="legend-dot is-failed" /> failed</span>
      </p>
    </section>
  )
}