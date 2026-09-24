# Flight Disruption Assistant — Project Spec

> Context file for continuing design/build work. Captures every decision made so far and the reasoning behind it.
> Status: design settled, build not started. Target build time: ~1–1.5 weeks.

---

## 1. What this is

A **multi-agent AI system** for a (fictional) airline that handles passengers whose flight has been **cancelled or heavily delayed**. The passenger talks to one assistant; behind it, a supervisor coordinates specialist agents and tools to:

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
2. **Specialists return structured data, never prose.** Only the supervisor talks to the passenger → one voice, one place to manage tone with angry customers, and specialists that are testable on their own.
3. **Shared case state is the single source of truth during the conversation.** All agents read from it; the supervisor updates it.
4. **Money is calculated by code, not by the LLM.** The LLM interprets messy facts; deterministic functions produce amounts and entitlements.
5. **Conversational order ≠ data dependency.** Components are wired by what data they actually need, not by the order topics come up in chat.
6. **The database is the source of truth; the LLM is the interface.** Actions are written as they're confirmed; confirmations are generated from DB records.
7. **Safety-critical escalation is not left to the LLM alone.** Deterministic triggers force escalation.

---

## 3. Architecture

```
                        Passenger
                           ⇅
┌──────────────────────────────────────────────────────────┐
│ SUPERVISOR (LLM)                                         │
│ - owns the conversation + case state                     │
│ - calls specialist agents as tools (in parallel if       │
│   independent), reads results, writes the reply          │
│ - calls care tools + escalation directly                 │
│ - records confirmed actions, closes case, sends confirm. │
└──────────────────────────────────────────────────────────┘
      │                     │                    │
      ▼                     ▼                    ▼
 REBOOKING AGENT      COMPENSATION AGENT     DIRECT TOOLS
 (LLM + inventory     (LLM + RAG +           - care tools
  tools)               compensation calc)    - escalateToHuman
                                             - DB write tools
```

### Supervisor pattern (chosen over a pure router)
Original idea was a router that returns the name of one agent per turn, with the answer going back through the router. **Refined** to a supervisor with specialists exposed **as tools**, because:
- A pure classifier breaks on multi-intent messages, e.g. *"My flight's cancelled, where do I sleep tonight and do I get money back?"* (two agents in one sentence)
- The supervisor can call several specialists in one turn, in parallel when independent
- One fewer hop per turn

---

## 4. Components

### 4.1 Supervisor — LLM
**Responsibilities**
- Converse with the passenger (tone, empathy, clarity)
- Maintain the case state
- Decide which specialists/tools to call and in what order, respecting data dependencies (e.g. passenger asks for a hotel first → supervisor realizes rebooking must happen first, because care depends on the new departure time)
- Present options, get passenger confirmation before any action
- Record confirmed actions, close the case, send confirmation

**Does NOT:** calculate compensation, decide care entitlements itself (calls the deterministic function), or improvise amounts.

### 4.2 Rebooking agent — LLM + tools
**Why it's an agent:** it must weigh options against what the passenger actually wants (*"I have a meeting at 9 tomorrow"*, *"I'd rather go via Munich than wait"*) and search again after rejections.

**Tools**
- `findAlternativeFlights(origin, destination, earliestDeparture, constraints)`
- `holdSeat(flightId, passengerId)` / `releaseSeat(...)` (optional)

**Returns (structured)**
```ts
{
  options: FlightOption[];
  recommended: string | null;   // flightId
  reason: string;
}
```

**Data source: `FlightInventory` interface with two implementations**
- **Mock (default):** JSON/DB table of the fictional airline's own flights, with planted demo scenarios (next flight full, only tomorrow morning, connection via another hub…). Makes demos and tests repeatable.
- **Duffel adapter (optional, switchable):** proves real-provider integration. Duffel test mode uses its sandbox airline "Duffel Airways"; test prices/schedules are not realistic.
- Note: Amadeus Self-Service (the usual tutorial choice) was decommissioned July 17, 2026 — not an option.
- Domain note: in reality an airline rebooks from **its own inventory**, not an open market search → the mock is actually more faithful to the real process.

### 4.3 Compensation agent — LLM + RAG + deterministic calculator
*(Renamed from "rights agent" — "rights" wrongly suggested it also covers care.)*

**Scope:** answers ONLY "is money owed, and how much?"

**Why it's an agent:** it must interpret messy facts against legal text (*"the captain said a technical issue — is that extraordinary?"*, *"strike by the airline's own staff?"*, *"fog?"*).

**Split**
- **RAG (interpretation):** index EU261 regulation text + EU Commission interpretative guidelines + a few CJEU ruling summaries. (The regulation alone would fit in the prompt; the guidelines and case summaries are what justify retrieval.)
- **Deterministic function (money):**
  ```ts
  calculateCompensation({
    distanceKm,
    arrivalDelayHours,
    isExtraordinary,
    // + cancellation notice / rerouting details as needed
  }) → { amountEur: 0 | 250 | 400 | 600 (or reduced), basis: string }
  ```
  The agent decides the **inputs**; the code decides the **amount**.

**Returns (structured)**
```ts
{
  eligible: boolean;
  amountEur: number;
  isExtraordinary: boolean;
  reasoning: string;
  sources: string[];   // retrieved passages used
}
```

**SQL note:** an LLM writing SQL to compute compensation was considered and rejected — the risk is LLM-generated logic around money, not the database itself.

### 4.4 Care — tools only (NOT an agent), owned by the supervisor
**Why demoted to tools:** care is lookups + actions with no real judgment. The only judgment (does the passenger need meals/hotel?) is a deterministic function of times and distance.

**Why the supervisor owns them (not the compensation agent)**
1. **Data dependency:** care depends on **rebooking** (new departure time) + booking (distance, original time). It does **not** depend on the compensation agent's output.
2. **Domain:** under EU261, extraordinary circumstances cancel the duty to pay **compensation** but **not** the duty of **care**. A storm-stranded passenger gets no €400 but still gets meals + hotel. Putting care inside the compensation agent risks leaking "extraordinary → nothing owed" into care and wrongly denying a hotel. Separation prevents this bug by design.
3. **Actions belong to whoever talks to the passenger:** booking a hotel / issuing a voucher happens after the passenger agrees. Sub-agents return assessments; they don't quietly act.

**Real dependency graph**
```
case state (booking, disruption, times)
   ├─→ rebooking agent ─→ new departure time ─┐
   │                                           ├─→ care function
   └───────────────────────────────────────────┘
   └─→ compensation agent ─→ compensation amount (only)
```

**Tools**
- `computeCareEntitlements(originalDeparture, newDeparture, distanceKm)` → `{ meals: MealEntitlement[], hotelNights: number, transport: boolean, communications: number }` (deterministic)
- `getCarePolicy(airport)` → airline policy (e.g. €15 meal voucher, partner hotels)
- `findPartnerHotel(airport, nights, constraints)`
- `issueVoucher(caseId, type, amount)`
- `bookHotel(caseId, hotelId, nights)`

### 4.5 Escalation — a tool, NOT an agent
- `escalateToHuman(caseId, reason)` → packages the case state into a handoff summary for a human operator
- Callable by the supervisor at its discretion
- **Deterministic triggers in code** force escalation regardless of the LLM, e.g.:
  - medical emergency keywords
  - unaccompanied minor / passenger with reduced mobility needing assistance
  - passenger rejected all proposed options twice
  - (optional) abusive conversation, legal threat, amount above a threshold

---

## 5. Case state (shared, in-conversation)

Draft shape — to be finalized:

```ts
type CaseState = {
  caseId: string;
  status: "open" | "escalated" | "closed";
  passenger: { id: string; name: string; contact: string; specialNeeds?: string[] };
  originalBooking: {
    bookingRef: string;
    flightNo: string;
    origin: string;
    destination: string;
    scheduledDeparture: string;   // ISO
    scheduledArrival: string;
    distanceKm: number;
  };
  disruption: {
    type: "cancellation" | "delay";
    announcedAt: string;
    statedCause?: string;         // from airline disruption record
    expectedDelayHours?: number;
  };
  rebooking?: {
    optionsOffered: FlightOption[];
    rejectedCount: number;
    confirmed?: FlightOption;     // new departure/arrival times live here
  };
  compensation?: {
    eligible: boolean;
    amountEur: number;
    isExtraordinary: boolean;
    reasoning: string;
  };
  care?: {
    entitlements: CareEntitlements;
    vouchersIssued: VoucherRef[];
    hotelBooked?: HotelBookingRef;
  };
  escalation?: { reason: string; at: string };
};
```

---

## 6. Database (Postgres — Supabase fits the stack)

**Principle:** the DB is the source of truth. The LLM reaches it only through **predefined, parameterized query tools** (the LLM picks the tool and arguments; the SQL is written by the developer). No string concatenation.

Example:
```ts
// Tool the LLM sees: getBooking({ bookingRef: string })
async function getBooking({ bookingRef }: { bookingRef: string }) {
  const { rows } = await db.query(
    `SELECT passenger_name, flight_no, origin, destination, scheduled_departure, distance_km
       FROM bookings
      WHERE booking_ref = $1`,
    [bookingRef]
  );
  return rows[0] ?? { error: "Booking not found" };
}
```

**Tables (draft)**
- `passengers`
- `bookings`
- `flights` (mock inventory, incl. seats available)
- `disruptions` (airline disruption records: flight, type, cause, times)
- `partner_hotels` (per airport, capacity)
- `care_policies` (voucher amounts per airport/meal type)
- `cases` (one per passenger disruption; status)
- `rebookings` (case → confirmed new flight)
- `compensation_decisions` (case → eligible, amount, reasoning)
- `vouchers` (case → type, amount)
- `hotel_bookings` (case → hotel, nights)
- `escalations` (case → reason, time)

**Query tools (draft)**
- `getBooking`, `getDisruption`, `findAlternativeFlights`, `findPartnerHotel`, `getCarePolicy`
- Write tools: `recordRebooking`, `recordCompensationDecision`, `issueVoucher`, `bookHotel`, `recordEscalation`, `closeCase`

---

## 7. End-of-case flow (decided)

1. **Write as you go:** each action is recorded **the moment the passenger confirms it** (rebooking confirmed → written; voucher issued → written). Reason: calls drop; a single write at the end could lose a hotel booking the airline is already paying for.
2. **Idempotent writes:** each write is safe to repeat (e.g. one voucher per case per meal type; unique constraints / upserts keyed on case + action), so retries never double-issue.
3. **Close the case:** set `cases.status = 'closed'`.
4. **Confirmation built from DB records:** read back the recorded rebooking, vouchers, hotel, compensation → the LLM only turns these records into a friendly message. The passenger is told exactly what was recorded, not what the model thinks happened.
5. **Stop there.** (The collected data can be reused later — see §10.)

**Full flow:** conversation → actions recorded as confirmed → case closed → confirmation built from records.

---

## 8. Typical conversation flow

1. Passenger opens chat (angry, possibly multi-intent)
2. Supervisor identifies the booking (`getBooking`) and disruption (`getDisruption`), initializes case state
3. Supervisor resolves intents respecting dependencies:
   - Rebooking → rebooking agent proposes options → passenger picks → `recordRebooking`
   - Care → `computeCareEntitlements` (needs new departure time) → offer → passenger accepts → `issueVoucher` / `bookHotel`
   - Compensation → compensation agent (can run in parallel with rebooking where it doesn't need the final arrival delay; otherwise after) → `recordCompensationDecision`
4. Deterministic escalation triggers checked every turn; `escalateToHuman` if hit
5. `closeCase` → confirmation generated from DB records → sent

---

## 9. EU261 reference (to encode; verify against regulation text + Commission guidelines before building)

**Compensation amounts (by flight distance)**
- ≤ 1,500 km → €250
- Intra-EU > 1,500 km, or other flights 1,500–3,500 km → €400
- > 3,500 km (non-intra-EU) → €600
- May be reduced by 50% if rerouting arrives within 2 / 3 / 4 hours (by distance band) of the original arrival
- Delays: compensation applies for **arrival delay ≥ 3 hours** (CJEU case law)
- Cancellations: rules depend on how far in advance the passenger was informed and the rerouting offered
- **Not owed** if caused by extraordinary circumstances that couldn't be avoided even with all reasonable measures

**Care (right to care)**
- Triggered by delay thresholds by distance band (roughly 2h / 3h / 4h) and during waits for rerouting after cancellation
- Meals and refreshments in reasonable relation to waiting time
- Hotel accommodation when an overnight stay (or extra stay) becomes necessary + transport between airport and hotel
- Two communications (calls/emails)
- **Owed even under extraordinary circumstances**

---

## 10. Out of scope for this build (possible later extension)

**Ops console with text-to-SQL agent** — for airline staff, NOT the passenger side.
- A passenger chat must never trigger model-written SQL.
- Separate entry point; shares the DB, not the conversation.
- Example questions: *"How many passengers from the cancelled Rome–Paris flight still aren't rebooked?"*, *"Compensation committed today by cause?"*, *"Which partner hotels near Fiumicino are near capacity?"*, *"Why were cases escalated this week?"*
- Guardrails: read-only Postgres role; access only to **views** (no passenger personal data); statement timeout + forced row limit; parse generated SQL and reject anything that isn't a single `SELECT`; show the generated SQL next to the answer.
- Demo idea: passenger chat and ops console side by side — passenger gets a hotel voucher on the left, "how many hotel nights issued today?" goes up by one on the right.
- Estimated cost: ~1.5–2 extra days.

**Interview answer on "how does your agent get data from an SQL database?"**
> "It depends on the use case. For customer-facing agents, I expose predefined, parameterized query tools: the LLM chooses which tool and which arguments, but the SQL is written by me. That keeps it safe from injection and predictable. For analytics, I might let the model generate SQL, but only against read-only views, with a least-privilege user, row limits, and validation."

---

## 11. Demo / portfolio must-haves

- **Live agent trace** in the UI: which agent/tool is running, in parallel where applicable, with inputs/outputs — the part people remember
- A visible **escalation moment** ("I'm not sure / this needs a human")
- A small **eval set** (e.g. 20 scripted scenarios with expected outcomes: correct rebooking constraints respected, correct compensation amount, care granted in extraordinary cases, escalation triggered) and a score
- Planted scenarios in mock data: storm (extraordinary → no compensation, still care), technical fault (compensation owed), next flight full, overnight wait (hotel), passenger rejecting options twice (escalation)

### Interview talking points
- Supervisor with agents-as-tools vs pure router (multi-intent messages)
- Specialists return data; one voice to the passenger
- Care demoted from agent to tools: "an agent is an LLM with decisions to make"
- Care separated from compensation because extraordinary circumstances cancel compensation but not care
- Money computed by code, interpreted by the LLM
- DB as source of truth; write-as-you-go; idempotent writes; confirmation from records
- Deterministic escalation triggers

---

## 12. Open decisions (not yet made)

- LLM provider/model(s) for supervisor and specialists
- Orchestration approach/framework vs hand-rolled loop
- Backend language/runtime (TypeScript likely, given the stack) and hosting
- Frontend (chat UI + trace panel)
- Vector store for the compensation agent's RAG
- Final case-state and DB schemas
- Whether the Duffel adapter is built in this iteration or later
