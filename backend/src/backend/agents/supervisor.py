from backend.models.chat import Message
from backend.models.case_state import CaseState
from pathlib import Path
import re
import litellm
from backend.tools.get_booking import GET_BOOKING_TOOL, get_booking
from backend.tools.get_disruption import GET_DISRUPTION_TOOL, get_disruption
from backend.agents.rebooking_agent import REBOOKING_AGENT_TOOL, get_flights_options
from backend.agents.compensation_agent import COMPENSATION_AGENT_TOOL, compensation_agent
from backend.tools.record_rebooking import RECORD_REBOOKING_TOOL, record_rebooking
from backend.tools.compute_care_entitlements import COMPUTE_CARE_ENTITLEMENTS_TOOL, compute_care_entitlements
from backend.tools.close_case import CLOSE_CASE_TOOL, close_case
from backend.tools.time_utils import to_local_time
from backend.observability import call_llm, record_guard, trace_tool
import json
from backend.state.case_file import (
    add_local_times,
    case_file_summary,
    find_case_by_flight,
    normalize_ref,
    update_case_file,
)

MODEL = "groq/openai/gpt-oss-120b"
FALLBACK_MODELS = [
    "gemini/gemini-3.5-flash-lite",
    "gemini/gemini-3.8-flash"
]
NUM_RETRIES = 0
MAX_TOOL_ROUNDS = 8  # safety limit: stop if the model keeps calling tools
MAX_EMPTY_REPLIES = 1  # how many times we ask the model again when it answers with nothing
MAX_REPLY_FIXES = 1  # how many times we ask the model to rewrite a reply that mentions an invented flight
PROMPT_PATH = Path(__file__).resolve().parent.parent / "prompts" / "supervisor_system.md"
SUPERVISOR_PROMPT = PROMPT_PATH.read_text(encoding="utf-8")
TOOL_SCHEMAS = [
    GET_BOOKING_TOOL,
    GET_DISRUPTION_TOOL,
    REBOOKING_AGENT_TOOL,
    RECORD_REBOOKING_TOOL,
    COMPUTE_CARE_ENTITLEMENTS_TOOL,
    COMPENSATION_AGENT_TOOL,
    CLOSE_CASE_TOOL,
]  # the model sees these tools and can call them

# Keys must match the "name" in each tool schema; values are the Python functions to run.
TOOL_FUNCTIONS = {
    "get_booking": get_booking,
    "get_disruption": get_disruption,
    "rebooking_agent": get_flights_options,
    "record_rebooking": record_rebooking,
    "compute_care_entitlements": compute_care_entitlements,
    "compensation_agent": compensation_agent,
    "close_case": close_case,
}

# Each tool's required arguments, read from its own schema: {"get_booking": ["booking_ref"], ...}
# A new tool is covered automatically, as long as its schema lists what it requires.
REQUIRED_ARGUMENTS = {
    schema["function"]["name"]: schema["function"]["parameters"].get("required", [])
    for schema in TOOL_SCHEMAS
}

# Tools that work on a whole case: the model passes only the booking reference,
# and the code hands them that booking's case.
CASE_TOOLS = {"compute_care_entitlements", "compensation_agent", "close_case"}

# An Aurora Airways flight number in a reply: "AU" followed by 3 or 4 digits, e.g. AU644
FLIGHT_NUMBER = re.compile(r"\bAU\d{3,4}\b")


def parse_arguments(arguments_json: str) -> dict | None:
    """The model's arguments, from JSON text to a dictionary. None if they aren't valid."""
    try:
        # It turns the model's arguments from text into a Python dictionary. from '{"booking_ref": "1254RF"}' to {"booking_ref": "1254RF"}
        arguments = json.loads(arguments_json or "{}")
    except json.JSONDecodeError:
        return None
    return arguments if isinstance(arguments, dict) else None


def missing_arguments(name: str, arguments: dict) -> list[str]:
    """The required arguments the model left out or left empty."""
    return [
        field
        for field in REQUIRED_ARGUMENTS.get(name, [])
        if arguments.get(field) in (None, "")
    ]


def known_flight_numbers(cases: dict[str, CaseState]) -> set[str]:
    """Every flight number that really exists in this conversation's case file.

    The original flights, the options found by the rebooking agent and the confirmed flights.
    Connections like "AU404/AU433" count as two flight numbers.
    """
    known = set()
    for case in cases.values():
        flights = []
        if case.original_booking:
            flights.append(case.original_booking.flight_no)
        flights.extend(option.flight_no for option in case.rebooking.options_offered)
        if case.rebooking.confirmed:
            flights.append(case.rebooking.confirmed.flight_no)
        for flight_no in flights:
            known.update(flight_no.split("/"))
    return known


def last_assistant_message(history: list[Message]) -> str:
    """The assistant's most recent message to the passenger, or "" if there isn't one."""
    for message in reversed(history):
        if message.role == "assistant":
            return message.content
    return ""


def confirmation_problem(arguments: dict | None, cases: dict[str, CaseState], history: list[Message]) -> dict | None:
    """Code-level check of Step 5: a flight can only be booked right after it was restated alone.

    The assistant's previous message must name the flight being booked, and no other offered
    flight. That message is the confirmation question; the passenger's current message answers it.
    A list of options, or a question about a different flight, is not a confirmation.
    Returns an error for the model, or None if the booking may go ahead.
    """
    if not arguments:
        return None
    case = cases.get(normalize_ref(arguments.get("booking_ref", "")))
    if case is None:
        return None  # run_tool's own checks will answer
    flight_id = str(arguments.get("flight_id", "")).strip().upper()
    chosen = next((o for o in case.rebooking.options_offered if o.flight_id == flight_id), None)
    if chosen is None:
        return None  # run_tool refuses flights that were never offered

    previous = last_assistant_message(history)
    names_chosen = all(number in previous for number in chosen.flight_no.split("/"))
    other_numbers = {
        number
        for option in case.rebooking.options_offered
        if option.flight_id != flight_id
        for number in option.flight_no.split("/")
    }
    names_others = any(number in previous for number in other_numbers)

    if names_chosen and not names_others:
        return None

    record_guard("supervisor", "booking_not_confirmed", f"{chosen.flight_no} was not confirmed on its own")
    return {
        "error": (
            f"The passenger has not confirmed {chosen.flight_no} yet. Do not book it now. First restate "
            f"this exact flight (flight number {chosen.flight_no}, date, local departure and arrival "
            "times, direct or via) and ask the passenger to confirm. Book only after they say yes."
        )
    }


def run_tool(name: str, arguments: dict | None, cases: dict[str, CaseState]) -> dict:
    function = TOOL_FUNCTIONS.get(name)
    if function is None:
        record_guard("supervisor", "unknown_tool", f"the model called '{name}'")
        return {"error": f"Unknown tool '{name}'. Use only the tools you were given."}

    if arguments is None:
        record_guard("supervisor", "invalid_arguments", f"{name} was called with arguments that aren't valid JSON")
        return {"error": "The tool arguments were not valid JSON."}

    # The model forgot the booking reference, but there's only one case: it can only mean that one.
    # Fill it in, so the tool runs and update_case_file also knows where to save the result.
    if name in CASE_TOOLS and not normalize_ref(arguments.get("booking_ref", "")) and len(cases) == 1:
        arguments["booking_ref"] = next(iter(cases))
        record_guard(
            "supervisor",
            "booking_ref_auto_filled",
            f"{name} called without booking_ref, used the only case {arguments['booking_ref']}",
        )

    # Every tool: refuse to run if a required argument is missing, and say which one
    missing = missing_arguments(name, arguments)
    if missing:
        record_guard("supervisor", "missing_arguments", f"{name} called without {', '.join(missing)}")
        hint = ""
        if "booking_ref" in missing and cases:
            hint = f" Known booking references: {', '.join(cases)}."
        return {
            "error": (
                f"You called {name} without its required argument(s): {', '.join(missing)}. "
                f"Call {name} again with all of them.{hint}"
            )
        }

    # Case tools: find the case for the booking reference, and pass the case itself
    if name in CASE_TOOLS:
        ref = normalize_ref(arguments["booking_ref"])
        case = cases.get(ref)
        if case is None:
            known = ", ".join(cases) or "none yet"
            record_guard("supervisor", "unknown_booking", f"{name} called for {ref}, which has no case")
            return {
                "error": (
                    f"No case found for booking '{ref}'. Call {name} again with booking_ref set to "
                    f"one of: {known}. If the booking isn't there yet, call get_booking first."
                )
            }
        try:
            return function(case)
        except Exception as e:
            return {"error": f"Tool '{name}' failed: {e}"}

    # Guard: this booking is already in the case file, so don't look it up again.
    # Return what's stored instead of running the tool (saves a call and tokens).
    if name == "get_booking":
        ref = normalize_ref(arguments["booking_ref"])
        case = cases.get(ref)
        if case and case.original_booking and case.passenger:
            record_guard("supervisor", "booking_from_case_file", f"get_booking({ref}) answered from the case file")
            return {
                "passenger": case.passenger.model_dump(mode="json"),
                "booking": case.original_booking.model_dump(mode="json"),
                "note": "Already in the case file; no need to call get_booking again.",
            }

    # Guard: only a flight the passenger was actually offered can be booked
    if name == "record_rebooking":
        case = cases.get(normalize_ref(arguments["booking_ref"]))
        offered = [option.flight_id for option in case.rebooking.options_offered] if case else []
        if str(arguments["flight_id"]).strip().upper() not in offered:
            record_guard("supervisor", "flight_not_offered", f"record_rebooking refused, {arguments['flight_id']} was never offered")
            return {
                "error": (
                    f"Flight {arguments['flight_id']} was not among the options found for this booking, "
                    f"so it can't be booked. Offered flight_ids: {', '.join(offered) or 'none yet'}. "
                    "Only book a flight the passenger was shown and confirmed."
                )
            }

    # The rebooking agent needs to know when the passenger was supposed to fly.
    # Code adds it from the case file; the model never has to send it.
    if name == "rebooking_agent":
        case = find_case_by_flight(cases, arguments["disrupted_flight_no"])
        if case and case.original_booking:
            booking = case.original_booking
            arguments["original_departure_local"] = to_local_time(booking.scheduled_departure, booking.origin)

    try:
        # turns that dictionary into named arguments and calls the function. For example, if arguments is {"booking_ref": "1254RF"}, it will call get_booking(booking_ref="1254RF").
        return function(**arguments)
    except Exception as e:
        return {"error": f"Tool '{name}' failed: {e}"}


def answer_request(message: str, history: list[Message] | None = None, cases: dict[str, CaseState] | None = None) -> str:

    if history is None:
        history = []

    if cases is None:
        cases = {}

    messages: list[dict] = [{"role": "system", "content": SUPERVISOR_PROMPT}]
    # The case file: facts from earlier tool results, which the history doesn't contain
    messages.append({"role": "system", "content": case_file_summary(cases)})
    messages.extend([m.model_dump() for m in history])

    messages.append({"role": "user", "content": message})

    empty_replies = 0  # how many times the model has answered with nothing in this turn
    reply_fixes = 0    # how many times the model was asked to rewrite a reply with an invented flight

    #range() generates a sequence of numbers for a loop.
    for _ in range(MAX_TOOL_ROUNDS):
        try:
            # Same as litellm.completion, but recorded in Langfuse and in the turn summary
            response = call_llm(
                agent="supervisor",
                model=MODEL,
                messages=messages,
                tools=TOOL_SCHEMAS,
                fallbacks=FALLBACK_MODELS,
                num_retries=NUM_RETRIES,
            )
        except litellm.BadRequestError as e:
            # Groq rejects calls to tools that weren't sent in TOOL_SCHEMAS.
            # Tell the model and let it try again, instead of crashing.
            if "tool_use_failed" in str(e):
                record_guard("supervisor", "tool_not_available", str(e)[:300])
                messages.append({
                    "role": "system",
                    "content": (
                        "You tried to call a tool that isn't available. Use only the tools "
                        "you were given, or answer the passenger directly."
                    ),
                })
                continue
            # Any other bad request is a real bug: let it crash so we see it
            raise
        except Exception as e:
            # Every model failed (rate limits, provider outage, timeout...): never crash the request.
            # Whatever the tools already did is saved in the case file, so the passenger can just retry.
            record_guard("supervisor", "all_models_failed", f"{type(e).__name__}: {str(e)[:300]}")
            return (
                "Sorry, I'm having trouble reaching our systems right now. "
                "Please send your message again in a minute."
            )
        reply = response.choices[0].message

        # No tool requested: the model is giving its final answer
        if not reply.tool_calls:
            text = (reply.content or "").strip()

            if text:
                # Output guardrail: the reply may only mention flights that exist in the case file
                invented = sorted(set(FLIGHT_NUMBER.findall(text)) - known_flight_numbers(cases))
                if not invented:
                    return text

                record_guard("supervisor", "invented_flight_in_reply", f"reply mentions {', '.join(invented)}")
                if reply_fixes < MAX_REPLY_FIXES:
                    reply_fixes += 1
                    messages.append({"role": "assistant", "content": text})
                    messages.append({
                        "role": "system",
                        "content": (
                            f"Your reply mentions flight(s) {', '.join(invented)}, which do not exist: "
                            "they are not in the case file or any tool result. Never invent flights. "
                            "Rewrite your reply using only flights from the case file. If the passenger "
                            "wants more options than the case file has, call rebooking_agent again."
                        ),
                    })
                    continue

                return (
                    "Sorry, I made a mistake while listing the flights. "
                    "Could you ask me again for the options?"
                )

            # The model ended its turn with an empty message. Never send that to the passenger:
            # ask it once to write its reply, based on what the tools just returned.
            if empty_replies < MAX_EMPTY_REPLIES:
                empty_replies += 1
                record_guard("supervisor", "empty_reply", "the model answered with nothing; asked it to write its reply")
                messages.append({
                    "role": "system",
                    "content": (
                        "Your last reply was empty. Write your reply to the passenger now, "
                        "based on the tool results above. Do not call any tools."
                    ),
                })
                continue

            return (
                "Sorry, something went wrong on my side while writing my reply. "
                "Could you send your last message again?"
            )

        # The model asked for one or more tools. First, record its request...
        messages.append({
            "role": "assistant",
            "content": reply.content or "",
            "tool_calls": [
                {
                    "id": call.id,
                    "type": "function",
                    "function": {
                        "name": call.function.name,
                        "arguments": call.function.arguments,
                    },
                }
                for call in reply.tool_calls
            ],
        })
        # ...then run each tool, save its facts to the right case, and add its result
        for call in reply.tool_calls:
            name = call.function.name
            arguments = parse_arguments(call.function.arguments)

            def run(name=name, arguments=arguments):
                # Step 5 in code: a booking needs the passenger's explicit confirmation of that exact flight.
                # If it's missing, the refusal is the result and the tool never runs.
                if name == "record_rebooking":
                    refusal = confirmation_problem(arguments, cases, history)
                    if refusal:
                        return refusal
                return run_tool(name, arguments, cases)

            # Runs the tool and records it (terminal, Langfuse, turn summary)
            result = trace_tool("supervisor", name, arguments, run)

            # Times are computed by code: add local times to the disruption before the model reads it
            if name == "get_disruption":
                add_local_times(cases, result)
            update_case_file(cases, name, arguments, result)
            messages.append({
                "role": "tool",
                "tool_call_id": call.id,
                "content": json.dumps(result, ensure_ascii=False)  #here we turn the Py dictionary result into a string, so the model can read it. For example, {"error": "The flight number is empty."} becomes '{"error": "The flight number is empty."}'
            })

        # Loop back: the model now sees the results and continues

    record_guard("supervisor", "max_rounds", f"no final reply after {MAX_TOOL_ROUNDS} rounds")
    return "I'm sorry, I'm having trouble completing this right now. Let me connect you with a colleague."