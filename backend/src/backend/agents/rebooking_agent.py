from backend.models.case_state import FlightOption, RebookingResult
from pathlib import Path
import litellm
from backend.tools.search_flights import SEARCH_FLIGHTS_TOOL, search_flights
from backend.tools.time_utils import current_local_time, to_local_time
from backend.observability import call_llm, record_guard, trace_tool
import json
from pydantic import ValidationError

MODEL = "groq/openai/gpt-oss-120b"
FALLBACK_MODELS = [
    "gemini/gemini-3.5-flash-lite",
    "gemini/gemini-3.8-flash"
]
NUM_RETRIES = 0
MAX_TOOL_ROUNDS = 6  # safety limit: stop if the model keeps calling tools
PROMPT_PATH = Path(__file__).resolve().parent.parent / "prompts" / "rebooking_system.md"
REBOOKING_PROMPT = PROMPT_PATH.read_text(encoding="utf-8")
TOOL_SCHEMAS = [SEARCH_FLIGHTS_TOOL]  # the model sees these tools and can call them

TOOL_FUNCTIONS = {
    "search_flights": search_flights,
}

def run_tool(name: str, arguments_json: str, earliest_departure: str = "") -> dict:
    function = TOOL_FUNCTIONS.get(name)
    if function is None:
        return {"error": f"Unknown tool '{name}'. Use only the tools you were given."}

    try:
        # It turns the model's arguments from text into a Python dictionary. from '{"booking_ref": "1254RF"}' to {"booking_ref": "1254RF"}
        arguments = json.loads(arguments_json or "{}")
    except json.JSONDecodeError:
        return {"error": "The tool arguments were not valid JSON."}

    # Code, not the model, decides the earliest search date: never before the original flight's day.
    # Both are local times written the same way ("2026-10-27 00:00"), so comparing the texts compares the times.
    if name == "search_flights" and earliest_departure:
        if arguments.get("depart_after", "") < earliest_departure:
            arguments["depart_after"] = earliest_departure

    try:
        # turns that dictionary into named arguments and calls the function. For example, if arguments is {"booking_ref": "1254RF"}, it will call get_booking(booking_ref="1254RF").
        return function(**arguments)
    except Exception as e:
        return {"error": f"Tool '{name}' failed: {e}"}
    
def keep_found_flights(answer: RebookingResult, found_flights: dict[str, dict]) -> RebookingResult:
    """Keep only the options that search_flights really returned, with their details from the search.

    The model's answer is checked against the database results of this run: an option whose
    flight_id was never found is dropped (it was invented), and every kept option is rebuilt
    from the search row, so its times and connection come from the database, not from the model.
    """
    kept = []
    for option in answer.options:
        row = found_flights.get(option.flight_id)
        if row is None:
            record_guard("rebooking", "invented_flight_dropped", f"{option.flight_id} ({option.flight_no}) was never found by search_flights")
            continue
        kept.append(FlightOption.model_validate(row))

    kept_ids = {option.flight_id for option in kept}
    recommended = answer.recommended_flight_id
    if recommended not in kept_ids:
        recommended = kept[0].flight_id if kept else None

    return RebookingResult(options=kept, recommended_flight_id=recommended, reason=answer.reason)

def drop_options_before_original(result: RebookingResult, original_departure_local: str) -> RebookingResult:
    """A new flight can't leave on an earlier day than the flight it replaces: drop any option that does.

    Same-day flights are kept, even if they leave a little earlier (a valid rerouting under EU261).
    Both times are local times at the same origin airport, written the same way by to_local_time
    ("2026-10-17 09:15"), so the first 10 characters are the date ("2026-10-17").
    """
    if not original_departure_local:
        return result

    original_date = original_departure_local[:10]
    kept = []
    for option in result.options:
        departure_local = to_local_time(option.departure, option.origin)
        if departure_local[:10] < original_date:
            record_guard(
                "rebooking",
                "option_before_original_dropped",
                f"{option.flight_no} leaves on {departure_local[:10]}, before the original flight's date ({original_date})",
            )
            continue
        kept.append(option)

    kept_ids = {option.flight_id for option in kept}
    recommended = result.recommended_flight_id
    if recommended not in kept_ids:
        recommended = kept[0].flight_id if kept else None

    return RebookingResult(options=kept, recommended_flight_id=recommended, reason=result.reason)

def add_local_times(result: RebookingResult) -> dict:
    # The agent copies UTC times into its answer. Passengers need the time
    # on the clocks at each airport, so the conversion is done here in code, never by the model.
    # This turns a Pydantic model instance back into a plain Python dict.
    data = result.model_dump(mode="json")

    # zip walks through both lists side by side: the dict to add fields to,
    # and the original FlightOption, which still has real datetimes to convert
    for option, flight in zip(data["options"], result.options):
        option["departure_local"] = to_local_time(flight.departure, flight.origin)
        option["arrival_local"] = to_local_time(flight.arrival, flight.destination)

    return data

def get_flights_options(
    origin: str,
    destination: str,
    disrupted_flight_no: str,
    preferences: str = "",
    special_needs: str = "",
    original_departure_local: str = "",
) -> dict:
    # original_departure_local is NOT in the tool schema: the model never sends it.
    # The supervisor's run_tool fills it in from the case file before calling this function.
    origin_clean = origin.strip().upper()
    destination_clean = destination.strip().upper()
    disrupted_flight_no_clean = disrupted_flight_no.strip().upper()
    preferences_clean = preferences.strip().lower()
    special_needs_clean = special_needs.strip().lower()

    parameters = {
        "origin": origin_clean,
        "destination": destination_clean,
        "disrupted_flight_no": disrupted_flight_no_clean,
        "preferences": preferences_clean,
        "special_needs": special_needs_clean,
    }

    if not parameters["origin"] or not parameters["destination"] or not parameters["disrupted_flight_no"]:
        return {"error": "origin, destination and disrupted_flight_no are required."}

    # Lets the model turn "tonight" or "tomorrow morning" into real dates, in local time
    parameters["current_local_time_at_origin"] = current_local_time(origin_clean)

    # When the passenger was supposed to fly, so "as soon as possible" is read from that date,
    # not from today (a flight cancelled 20 days ahead shouldn't be replaced by one tonight)
    if original_departure_local:
        parameters["original_departure_local"] = original_departure_local

    parameters_json = json.dumps(parameters, ensure_ascii=False)

    messages: list[dict] = [{"role": "system", "content": REBOOKING_PROMPT}]
    messages.append({"role": "user", "content": parameters_json})

    # Every flight search_flights returns during this run, by flight_id: the only flights
    # the agent is allowed to put in its answer
    found_flights: dict[str, dict] = {}

        # The earliest the search may start: the start of the original flight's day
    earliest_departure = original_departure_local[:10] + " 00:00" if original_departure_local else ""

    for _ in range(MAX_TOOL_ROUNDS):
        try:
            # Same as litellm.completion, but recorded in Langfuse and in the turn summary
            response = call_llm(
                agent="rebooking",
                model=MODEL,
                messages=messages,
                tools=TOOL_SCHEMAS,
                fallbacks=FALLBACK_MODELS,
                num_retries=NUM_RETRIES,
            )
        except litellm.BadRequestError as e:
            if "tool_use_failed" in str(e):
                record_guard("rebooking", "tool_not_available", str(e)[:300])
                messages.append({
                    "role": "system",
                    "content": "You tried to call a tool that isn't available. Use only the tools you were given"
                })
                continue
            raise
        reply = response.choices[0].message

        if not reply.tool_calls:
            try:
                #This parses the LLM's reply as JSON and validates it against your Pydantic model in one step.
                answer = RebookingResult.model_validate_json(reply.content or "")
            except ValidationError as e:
                record_guard("rebooking", "invalid_answer_format", f"{e.error_count()} problem(s) in the JSON answer")
                # Show the model its own reply and what's wrong with it, then let it try again.
                messages.append({"role": "assistant", "content": reply.content or ""})
                messages.append({
                    "role": "user",
                    "content": (
                        "Your reply does not match the required format. "
                        "Reply again with only the JSON object, no other text.\n"
                        f"Problems found:\n{e}"
                    ),
                })
                continue
            # Only flights the search really found, with their details from the database
            answer = keep_found_flights(answer, found_flights)
            # Never offer a flight that leaves before the one it replaces
            answer = drop_options_before_original(answer, original_departure_local)
            # A plain dict with local times added: the supervisor's json.dumps turns it into text for its model.
            return add_local_times(answer)

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
        for call in reply.tool_calls:
            # Runs the tool and records it (terminal, Langfuse, turn summary)
            result = trace_tool(
                "rebooking",
                call.function.name,
                call.function.arguments,
                lambda call=call: run_tool(call.function.name, call.function.arguments, earliest_departure),
            )
            # Remember every real flight the search returned
            for flight in result.get("flights", []):
                found_flights[flight["flight_id"]] = flight
            messages.append({
                "role": "tool",
                "tool_call_id": call.id,
                "content": json.dumps(result)
            })
    record_guard("rebooking", "max_rounds", f"no valid answer after {MAX_TOOL_ROUNDS} rounds")
    return {"error": "I can't complete the request."}


REBOOKING_AGENT_TOOL = {
    "type": "function",
    "function": {
        "name": "rebooking_agent",
        "description": (
            "Specialist agent that searches the airline's flight inventory for alternatives to a "
            "disrupted flight and returns up to 7 options, ranked best first, with local departure "
            "and arrival times, a recommended flight and a short reason. "
            "Call it once you have the booking and the disruption. Pass the passenger's preferences "
            "in their own words if they gave any, and their special needs from the booking. "
            "Call it again with updated preferences if the passenger rejects the options."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "origin": {
                    "type": "string",
                    "description": "Origin airport IATA code from the booking, e.g. FCO.",
                },
                "destination": {
                    "type": "string",
                    "description": "Destination airport IATA code from the booking, e.g. CDG.",
                },
                "disrupted_flight_no": {
                    "type": "string",
                    "description": "The cancelled or delayed flight number, e.g. AU610.",
                },
                "preferences": {
                    "type": "string",
                    "description": "The passenger's timing or routing wishes in their own words. Empty if none.",
                },
                "special_needs": {
                    "type": "string",
                    "description": "Special needs from the booking, e.g. wheelchair assistance. Empty if none.",
                },
            },
            "required": ["origin", "destination", "disrupted_flight_no"],
        },
    },
}