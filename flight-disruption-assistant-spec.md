# Flight Disruption Assistant — Project Spec

> Context file for continuing design/build work. Captures every decision made so far, the reasoning behind it, and what is actually built.
> Status: **build in progress** (started 2026-09-24). Target build time: ~1–1.5 weeks.

---

## 1. What this is

A **multi-agent AI system** for a fictional airline, **Aurora Airways** (flight prefix `AU`), that handles passengers whose flight has been **cancelled or heavily delayed**. The passenger talks to one assistant; behind it, a supervisor coordinates specialist agents and tools to:

1. **Rebook** the passenger onto an alternative flight
2. Determine whether **EU261 compensation** is owed, and how much
3. Provide **care** (meals, hotel, transport) as required
4. **Escalate to a human** when needed
5. **Record** every confirmed action in a database and send a **confirmation** built from those records

**Purpose:** portfolio project for AI agent engineering roles. It must look like a production system, not a toy: bounded scope, clear reason for each component, safety and correctness designed in.

### Why this use case
- Real, current problem that airlines spend money on; everyone in Europe understands it instantly
- Bounded: we resolve **one broken trip**, not plan open-ended travel
- EU261 is a **finite ruleset** that can be encoded
- Conversations are non-linear (angry passengers jump between topics), which justifies a supervisor

---

## 2. Design principles (the "why" behind every decision)

1. **An agent = an LLM that makes decisions.** If a component only looks things up and executes, it is **tools**, not an agent. Wrapping tools in an LLM adds latency, cost and failure points.
2. **Specialists return structured data, never prose.** Only the supervisor talks to the passenger → one voice, one place to manage tone, and specialists that are testable on their own.
3. **Sub-agents assess; they never act.** Actions (booking, vouchers) happen only after the passenger agrees, through the supervisor.
4. **Shared case state is the single source of truth during the conversation.** All agents read from it; the supervisor updates it.
5. **Code does the maths, the LLM interprets and presents.** Money (compensation), entitlements (care), and time-zone conversion are computed by deterministic code, never by the model.
6. **Rules are enforced by code/data, not left to model judgment.** E.g. the `available_flights` view hides departed and full flights, so the agent can never offer them.
7. **Conversational order ≠ data dependency.** Components are wired by what data they actually need, not by the order topics come up in chat.
8. **The database is the source of truth; the LLM is the interface.** Actions are written as they're confirmed; confirmations are generated from DB records.
9. **Safety-critical escalation is not left to the LLM alone.** Deterministic triggers force escalation.
10. **Errors are returned to the model, not raised.** Tools return `{"error": ...}` so the model can read the problem and recover (fix a query, ask the passenger again).

---

## 3. Architecture

```
                        Passenger
                           ⇅
┌──────────────────────────────────────────────────────────┐
│ SUPERVISOR (LLM)                                         │
│ - owns the conversation (+ case state, not yet wired)    │
│ - calls specialist agents as tools, reads results,       │
│   writes the reply                                       │
│ - calls lookup, care, write and escalation tools         │
└──────────────────────────────────────────────────────────┘
      │                     │                    │
      ▼                     ▼                    ▼
 REBOOKING AGENT      COMPENSATION AGENT     DIRECT TOOLS
 (LLM + text-to-SQL   (LLM + RAG +           - get_booking, get_disruption
  on flight           compensation calc)     - care tools
  inventory)                                 - escalate_to_human
                                             - DB write tools
```

### Supervisor pattern (chosen over a pure router)
Original idea: a router returning the name of one agent per turn. **Refined** to a supervisor with specialists exposed **as tools**, because:
- A pure classifier breaks on multi-intent messages (*"My flight's cancelled, where do I sleep tonight and do I get money back?"*)
- The supervisor can call several specialists in one turn
- One fewer hop per turn

### Agents as tools (how it works in code)
From the supervisor's point of view an agent **is** a tool: a schema in `TOOL_SCHEMAS`, a function in `TOOL_FUNCTIONS`, called via `tool_calls`, result returned as a `tool` message. The only difference is inside the function: an agent runs **its own LLM loop** with its own prompt, its own tools, and a brand-new conversation (it never sees the passenger's chat, only the facts it needs).

### The tool-calling loop (both agents)
1. Send messages + tool schemas to the LLM
2. If the reply has no `tool_calls` → it's the answer, return it
3. Otherwise append the assistant's tool-call message, run each tool, append each result as a `tool` message linked by `tool_call_id`
4. Loop back; stop after `MAX_TOOL_ROUNDS` (safety limit) with a fallback message

Safety net: if the provider rejects a call to a tool that isn't in the schemas (Groq `tool_use_failed`), catch `litellm.BadRequestError`, append a system note ("use only the tools you were given") and continue, instead of returning a 500.

---

## 4. Components

### 4.1 Supervisor — LLM ✅ (partly built)
**Responsibilities**
- Converse with the passenger (tone, empathy, short messages)
- Decide which specialists/tools to call and in what order, respecting data dependencies
- **Before calling the rebooking agent, ask the passenger in one short message about preferences and assistance needs** (confirm special needs already in the booking instead of asking from scratch). Decided: no unsolicited option dumps; keep interactions short.
- Present options, get passenger confirmation before any action
- Record confirmed actions, close the case, send confirmation
- **Always show local times** (`departure_local` / `arrival_local`), never UTC

**Does NOT:** calculate compensation, decide care entitlements itself, convert time zones, or improvise amounts.

**Built:** `agents/supervisor.py` — loop, `run_tool`, safety net, tools `get_booking`, `get_disruption`, `rebooking_agent`. Prompt: `prompts/supervisor_system.md`.

### 4.2 Rebooking agent — LLM + text-to-SQL ✅ (built)
**Why it's an agent:** it must translate the passenger's wishes (*"direct only"*, *"not via Frankfurt"*, *"land before 9"*) into a search, weigh options (arrival time, connections, special needs) and search again after rejections.

**Python function:** `get_flights_options(origin, destination, disrupted_flight_no, preferences="", special_needs="")` in `agents/rebooking_agent.py`, exposed to the supervisor under the tool name **`rebooking_agent`**.

**Its only tool:** `run_flight_query(sql)` — the agent writes a SQLite `SELECT` on the `available_flights` view (see §6).

**Choice rules (in its prompt):** filter by origin/destination, exclude the disrupted flight, order by **arrival**; max 3 options; prefer earliest arrival; prefer **direct** flights for passengers with reduced mobility / assistance needs (verified: recommends direct AU903 over earlier-arriving connections for the wheelchair passenger); relax the least important constraint if nothing is found; never invent flights.

**Returns (validated with Pydantic `RebookingResult`):**
```python
{
  "options": [FlightOption + departure_local + arrival_local],  # max 3
  "recommended_flight_id": str | None,
  "reason": str,
}
```
If the model's JSON doesn't validate, its reply + the validation error are sent back and it retries (format retries count as rounds).

**Local times:** added **in code** after validation (`add_local_times`), using `tools/time_utils.py`. The agent itself works only in UTC (needed for its SQL comparisons) and its prompt says nothing about local time.

**Data source:** the airline's own mock inventory (an airline rebooks from **its own** flights, not an open market search, so the mock is faithful to reality).
- Duffel adapter: still an optional idea, not planned for this iteration.
- Amadeus Self-Service was decommissioned July 17, 2026 — not an option.

### 4.3 Compensation agent — LLM + RAG + deterministic calculator ⏳ (not built)
*(Renamed from "rights agent" — "rights" wrongly suggested it also covers care.)*

**Scope:** answers ONLY "is money owed, and how much?"

**Why it's an agent:** it must interpret messy facts against legal text (*"technical issue — extraordinary?"*, *"strike by the airline's own crew?"*, *"storm?"*).

**Split**
- **RAG (interpretation):** EU261 regulation text + EU Commission interpretative guidelines + a few CJEU ruling summaries. (The regulation alone fits in a prompt; the guidelines and case summaries justify retrieval.)
- **Deterministic function (money):** `calculate_compensation(distance_km, arrival_delay_minutes, is_extraordinary, ...)` → `{amount_eur, basis}`. The agent decides the **inputs**; the code decides the **amount**.

**Returns (structured):** `eligible`, `amount_eur`, `is_extraordinary`, `reasoning`, `sources`.

### 4.4 Care — tools only (NOT an agent), owned by the supervisor ⏳ (not built)
**Why tools:** lookups + actions, no real judgment. Whether meals/hotel are owed is a deterministic function of times and distance.

**Why the supervisor owns them (not the compensation agent)**
1. **Data dependency:** care depends on **rebooking** (new departure time) + booking (distance, original time), not on compensation.
2. **Domain:** extraordinary circumstances cancel **compensation** but **not care**. Keeping them separate prevents an "extraordinary → nothing owed" conclusion from leaking into care.
3. **Actions belong to whoever talks to the passenger.**

**Dependency graph**
```
case state (booking, disruption, times)
   ├─→ rebooking agent ─→ new departure time ─┐
   │                                           ├─→ care function
   └───────────────────────────────────────────┘
   └─→ compensation agent ─→ compensation amount (only)
```

**Tools (planned):** `compute_care_entitlements` (deterministic), `get_care_policy`, `find_partner_hotel`, `issue_voucher`, `book_hotel`.

### 4.5 Escalation — a tool, NOT an agent ⏳ (not built)
- `escalate_to_human(case_id, reason)` → packages the case state into a handoff summary
- **Deterministic triggers in code** force escalation, e.g. medical emergency keywords, unaccompanied minor, options rejected twice, **statements suggesting a risk of self-harm** (identified gap: a test message "I want to die!" was ignored; prompt must respond to that first, briefly and warmly, and code must escalate real risk)
- **Decided:** special needs (e.g. reduced mobility) do **not** trigger a handoff by themselves; the agents handle them (e.g. direct flights preferred). Hand off to a human **only if the passenger insists, or if no solution can be found**.

### 4.6 Write tools ⏳ (next)
- **`record_rebooking(case_id, flight_id)`** — next to build. Pre-written, parameterized SQL. Saves the confirmed flight and **reduces `seats_available` by 1**.
- Then: `record_compensation_decision`, `close_case`, voucher/hotel writes.
- **Writes never use model-written SQL.** Only the LLM chooses which tool and which arguments.

---

## 5. Case state

**Defined, not yet wired into the supervisor.** Currently `main.py` only keeps per-session message history in memory (`conversations: dict[session_id, list[Message]]`, user/assistant only — never the system prompt). The case state must be stored per session and loaded/saved each turn, because each HTTP request is stateless.

Defined in `models/case_state.py` (Pydantic):

```python
Passenger:        id, name, contact, special_needs: list[str]
OriginalBooking:  booking_ref, flight_no, origin, destination,
                  scheduled_departure: datetime, scheduled_arrival: datetime, distance_km
Disruption:       flight_no, type: "cancellation"|"delay", announced_at: datetime,
                  stated_cause, expected_delay_minutes: int | None
FlightOption:     flight_id, flight_no, origin, destination, via: str | None,
                  departure: datetime, arrival: datetime
Rebooking:        options_offered: list[FlightOption], rejected_count, confirmed: FlightOption | None
Compensation:     eligible, amount_eur, is_extraordinary, reasoning, sources
CareEntitlements: meals, hotel_nights, transport, communications
Voucher, HotelBooking, Care, Escalation
CaseState:        case_id, status: "open"|"escalated"|"closed", passenger, original_booking,
                  disruption, rebooking, compensation, care, escalation
RebookingResult:  options: list[FlightOption], recommended_flight_id, reason   (agent output)
```

Notes:
- `RebookingResult` = one answer from the agent (momentary). `Rebooking` = the record across the conversation (offered, rejected count, confirmed). Both are needed.
- `rebooking` and `care` default to empty objects (they fill gradually); `compensation` and `escalation` default to `None` (they arrive all at once).
- Delays are stored in **minutes** (exact; EU261 hour thresholds are compared as minutes).
- Disruption has `flight_no`: a disruption is a fact about a flight, needed for the future DB table and ops queries.

---

## 6. Data & database

### Mock data (`backend/data/`)
All times are stored **relative to "now"** (offsets in minutes), converted to real UTC datetimes when loaded, so the demo always shows flights for today/tomorrow.

- **`bookings.json`** — 3 passengers/bookings: Marco Rossi (AU610 FCO→CDG, 1,105 km), Sophie Martin (AU224 CDG→LIS, 1,455 km), Lukas Weber (AU901 FCO→JFK, 6,880 km, wheelchair assistance). Fields: `departure_offset_minutes`, `duration_minutes`.
- **`disruptions.json`** — one per flight, causes worded like an ops record (never as legal labels):
  - AU610: cancelled, technical fault (hydraulics) → compensation expected
  - AU224: delayed 300 min, thunderstorms at CDG → extraordinary: no compensation, care still owed
  - AU901: cancelled, cabin-crew industrial action → generally *not* extraordinary under CJEU rulings
- **`flights.json`** — 13-flight inventory with planted scenarios: a full flight, an already-departed flight, a Milan connection, tomorrow-morning directs, LHR/FRA connections to JFK (FRA has 1 seat), a distractor route (FCO→LIS). Fields include `via` and `seats_available`.
  - Note: the Milan connection (F-004) currently arrives ~30 min *earlier* than AU614. To test "leaves sooner but arrives later", raise its `duration_minutes` from 285 to ~360.

### Demo booking references
`get_booking` accepts **any** reference: exact match on a known ref first (to pick a scenario on purpose, e.g. for evals/interviews), otherwise a **SHA-256 hash** of the reference picks a booking deterministically (same ref → same booking, even across restarts). The returned booking carries the reference the visitor typed.

### Flight inventory database (SQLite, for now)
- `db/inventory.py` → `build_inventory_db()` rebuilds `data/inventory.db` from `flights.json` at **every server start** (FastAPI **lifespan** in `main.py`). The `.db` file is generated and gitignored.
- Table `flights` + view **`available_flights`** = flights with `departure > datetime('now') AND seats_available > 0`.
- Dates stored as text in **SQLite's own format** `'YYYY-MM-DD HH:MM:SS'`, **UTC**, so comparisons with `datetime('now')` work.
- Later: move to Postgres/Supabase; the SQL stays mostly the same.

### Text-to-SQL (`tools/run_flight_query.py`) — decided and built
**Decision changed from the original plan:** text-to-SQL is used **on the passenger side**, but **only for reads, only on public flight data**, inside the rebooking agent. Passengers express flight wishes in open-ended ways a fixed search tool can't cover. Writes always use pre-written SQL.

Guardrails:
1. Connection opened **read-only** (`file:...?mode=ro`) — the real guarantee
2. Must start with `SELECT`; no `;` except a trailing one (blocks stacked statements)
3. The raw `flights` table is rejected (regex `\bflights\b`); only `available_flights` is allowed
4. At most 20 rows (`fetchmany`)
5. SQL errors returned as `{"error": ...}` so the agent fixes its own query
- Worst case of a manipulated query: reading public flight schedules.
- Table description (columns, formats, example) lives in the **rebooking prompt** (instructions); the tool's schema description is one short descriptive sentence.
- Possible extra layer later: SQLite authorizer (`set_authorizer`).

### Time zones (`tools/time_utils.py`)
- Everything stored and compared in **UTC**.
- `AIRPORT_TIMEZONES` maps each airport to its zone (FCO/MXP Europe/Rome, CDG Europe/Paris, LIS Europe/Lisbon, MAD Europe/Madrid, FRA Europe/Berlin, LHR Europe/London, JFK America/New_York).
- `to_local_time(utc_time, airport)` uses `zoneinfo` (handles summer/winter time); unknown airport → UTC, clearly labelled. Requires `tzdata` on Windows.
- To do: also add local times to `get_booking` results.

### Planned production DB (Postgres/Supabase)
Tables (draft): `passengers`, `bookings`, `flights`, `disruptions`, `partner_hotels`, `care_policies`, `cases`, `rebookings`, `compensation_decisions`, `vouchers`, `hotel_bookings`, `escalations`.

---

## 7. End-of-case flow (decided)

1. **Write as you go:** each action is recorded the moment the passenger confirms it (calls drop; a single write at the end could lose a booking the airline is paying for).
2. **Idempotent writes:** safe to repeat (e.g. one voucher per case per meal type; unique constraints/upserts).
3. **Close the case:** status → closed.
4. **Confirmation built from DB records:** the LLM only turns recorded facts into a friendly message.
5. **Stop there.** The collected data can be reused later (§11).

---

## 8. Typical conversation flow

1. Passenger opens chat (angry, possibly multi-intent)
2. Supervisor asks for the booking reference → `get_booking` → `get_disruption` (with the flight number from the booking)
3. Supervisor explains what happened, asks **one** short question about preferences and assistance needs
4. `rebooking_agent` → options with local times → passenger picks → `record_rebooking`
5. Care → `compute_care_entitlements` → offer → passenger accepts → `issue_voucher` / `book_hotel`
6. Compensation → compensation agent → `record_compensation_decision`
7. Deterministic escalation triggers checked every turn
8. `close_case` → confirmation from DB records

If `get_disruption` finds no disruption (e.g. passenger missed the flight), the flight operated normally: the assistant explains politely that it handles cancellations and delays only.

---

## 9. EU261 reference (to encode; verify against regulation text + Commission guidelines before building)

**Compensation amounts (by flight distance)**
- ≤ 1,500 km → €250
- Intra-EU > 1,500 km, or other flights 1,500–3,500 km → €400
- > 3,500 km (non-intra-EU) → €600
- May be reduced by 50% if rerouting arrives within 2 / 3 / 4 hours (by distance band) of the original arrival
- Delays: compensation for **arrival delay ≥ 3 hours** (CJEU case law)
- Cancellations: rules depend on how far in advance the passenger was informed and the rerouting offered
- **Not owed** for extraordinary circumstances that couldn't be avoided even with all reasonable measures

**Care (right to care)**
- Triggered by delay thresholds by distance band (roughly 2h / 3h / 4h) and during waits for rerouting after cancellation
- Meals and refreshments in reasonable relation to waiting time
- Hotel when an overnight stay becomes necessary + transport to/from it
- Two communications (calls/emails)
- **Owed even under extraordinary circumstances**

---

## 10. Tech stack & project setup (decided)

- **Repo:** `flight-disruption-assistant/` with `frontend/` and `backend/`, one Git repo, synced with GitHub Desktop.
- **Frontend:** React + TypeScript with **Vite** (ESLint). Vite dev proxy `/api` → `localhost:8000`. Not built yet (chat UI + trace panel planned).
- **Backend:** Python 3.12, **FastAPI**, managed with **uv** (`pyproject.toml` + `uv.lock`; `uv sync` on a new machine). Package layout `backend/src/backend/`:
  ```
  backend/
  ├── data/            bookings.json, disruptions.json, flights.json, inventory.db (generated)
  ├── .env             GROQ_API_KEY, GEMINI_API_KEY (never committed)
  ├── test_*.py        standalone test scripts
  └── src/backend/
      ├── main.py      lifespan (builds inventory), POST /chat, GET /health
      ├── agents/      supervisor.py, rebooking_agent.py
      ├── tools/       get_booking.py, get_disruption.py, run_flight_query.py, time_utils.py
      ├── db/          inventory.py
      ├── models/      case_state.py, chat.py
      └── prompts/     supervisor_system.md, rebooking_system.md
  ```
- **API:** `POST /chat` with `{"message", "session_id"}` → `{"reply", "session_id"}` (snake_case everywhere, no aliases).
- **Run:** `uv run uvicorn backend.main:app --reload --reload-include "*.md" --port 8000` (prompts are read at import, so `.md` changes need a reload).
- **Conventions:** one file per tool with its function + schema; schemas describe what a tool does (descriptive voice), prompts give instructions; readable code over clever one-liners; `model_validate` for dict data; `#` comments (never bare triple-quoted strings inside data structures).

### LLM access
- **LiteLLM** as the model gateway (one call format for all providers; switching = changing the model string). Installed version is safe (the March 24, 2026 PyPI compromise affected 1.82.7/1.82.8 only); `uv.lock` pins it.
- Orchestration: **hand-rolled loop** (no agent framework).
- Each call uses `num_retries` and `fallbacks` (cross-provider), e.g. Groq `openai/gpt-oss-120b` ↔ Gemini Flash / Flash-Lite. Model strings must be plain strings (a trailing comma once turned one into a tuple and broke the fallbacks).
- **Lessons from free tiers:** Groq free = 8K tokens/min and 200K/day on every chat model (too tight for multi-call turns); Gemini free tier gets 503 "high demand" at peak hours (US mornings), newest models worst; Hugging Face free credit ($0.10/month) is useless here. `gpt-oss` sometimes invents non-existent tools (e.g. `repo_browser.open_file`).
- **To do:** a small paid tier before any live demo; LiteLLM **Router** with cooldowns so a failing model is skipped for a while instead of retried on every call; proper model selection (tool-calling reliability is the main criterion).

---

## 11. Out of scope for this build (possible later extension)

**Ops console with text-to-SQL for airline staff** — separate entry point, shares the DB, not the conversation.
- Example questions: *"How many passengers from the cancelled Rome–Paris flight still aren't rebooked?"*, *"Compensation committed today by cause?"*, *"Which partner hotels near Fiumicino are near capacity?"*
- Same guardrails as `run_flight_query`, plus views without passenger personal data.
- Demo idea: passenger chat and ops console side by side.
- Estimated cost: ~1.5–2 extra days.

**Interview answer on "how does your agent get data from an SQL database?"**
> "It depends on what the query does and who's asking. Writes and anything touching money go through predefined, parameterized tools: the LLM chooses the tool and the arguments, but I write the SQL. For flexible reads, like a passenger saying 'direct only, not via Frankfurt, land before 9', the agent writes its own SELECT — but against a read-only connection, on a view that only exposes bookable public flight data, with SELECT-only validation and a row limit. The worst a manipulated query can do is read a flight schedule."

---

## 12. Demo / portfolio must-haves

- **Live agent trace** in the UI (which agent/tool ran, with inputs/outputs, including the SQL the rebooking agent wrote)
- A visible **escalation moment**
- A small **eval set** (e.g. 20 scripted scenarios with expected outcomes) and a score — start from the existing `test_*.py` scripts
- Planted scenarios (in the mock data): storm → no compensation but care; technical fault → compensation; crew strike → tricky legal case; full next flight; departed flight; wheelchair passenger → direct flight preferred; options rejected twice → escalation

### Interview talking points
- Supervisor with agents-as-tools vs pure router (multi-intent messages)
- Specialists return data; one voice to the passenger; sub-agents never act
- Care demoted from agent to tools: "an agent is an LLM with decisions to make"
- Care separated from compensation (extraordinary circumstances cancel compensation, not care)
- Code does the maths: compensation, care entitlements, time zones
- Text-to-SQL for flexible reads vs pre-written SQL for writes — and why
- Business rules enforced by a DB view, not by the prompt
- Errors returned to the model so it self-corrects (SQL errors, JSON validation retries)
- Retries + cross-provider fallbacks; lessons from free-tier outages
- DB as source of truth; write-as-you-go; idempotent writes; confirmation from records
- Deterministic escalation triggers

---

## 13. Build status & next steps

**Done**
- Project setup (Vite frontend scaffold, uv/FastAPI backend, GitHub)
- `POST /chat` with per-session history
- Supervisor with tool loop, `tool_use_failed` safety net, retries + fallbacks
- Tools: `get_booking` (any reference, relative times), `get_disruption`
- SQLite inventory built at startup + `available_flights` view
- `run_flight_query` with guardrails (tested: valid query, non-SELECT, raw table, SQL typo)
- Rebooking agent (text-to-SQL, JSON validation with retry), wired into the supervisor and working end to end
- Supervisor asks for preferences/needs before rebooking
- Local-time conversion (`time_utils.py`) — in progress: verify output, add the supervisor prompt rule

**Next**
1. Finish/verify local times (+ add them to `get_booking`)
2. `record_rebooking` (first write tool, decrements seats)
3. Wire the case state into the supervisor (stored per session)
4. Care tools + `compute_care_entitlements`
5. Compensation agent (RAG + calculator)
6. Escalation tool + deterministic triggers (incl. self-harm statements)
7. `close_case` + confirmation from records
8. Frontend chat UI + trace panel
9. Eval set
10. Refactor: one shared tool-loop helper for both agents; model selection; LiteLLM Router with cooldowns

**Known issues / tweaks (deferred: prompt fine-tuning later)**
- Supervisor sometimes asks only about preferences, not assistance needs
- Phrasing like "AU610 has been replaced by AU614" before anything is booked
- Remove debug `print` lines and the unused `history = []` in `rebooking_agent.py` before publishing


# Tool result shapes

What `result` looks like in `supervisor.py`, depending on which tool ran:

```python
result = run_tool(call.function.name, call.function.arguments, case)
```

`run_tool` is a dispatcher: the shape of `result` depends on `call.function.name`, so at that line it can only be typed as `dict`. The shape becomes known in `update_case_file`, inside the branch for each tool, where the dict is converted into a Pydantic model.

| Tool | `result` looks like | Converted in `update_case_file` to |
|---|---|---|
| `get_booking` | `{"passenger": {...}, "booking": {...}}` | `Passenger` + `OriginalBooking` |
| `get_disruption` | `{"flight_no", "type", "announced_at", "stated_cause", "expected_delay_minutes"}` | `Disruption` |
| `rebooking_agent` | `{"options": [...], "recommended_flight_id", "reason"}` | a list of `FlightOption` |
| `record_rebooking` | `{"booking_ref", "booked_at", "flight_id", "flight_no", "origin", "destination", "via", "departure", "arrival", "departure_local", "arrival_local", "status"}` | `FlightOption` (confirmed flight) |
| any tool, on failure | `{"error": "..."}` | nothing (the case file is not changed) |

## Inside the results

**`get_booking`**
```python
{
    "passenger": {"id", "name", "contact", "special_needs": [...]},
    "booking": {"booking_ref", "flight_no", "origin", "destination",
                "scheduled_departure", "scheduled_arrival", "distance_km"},
}
```

**`rebooking_agent`**, each item in `"options"`:
```python
{"flight_id", "flight_no", "origin", "destination", "via",
 "departure", "arrival", "departure_local", "arrival_local"}
```

## The shape journey

```
JSON file / model arguments (text)
   │  json.loads
   ▼
dict ──── model_validate ────► Pydantic object (dot access, checked)
   ▲                                │
   └──────── model_dump ────────────┘
   │  json.dumps
   ▼
text (sent to the model)
```