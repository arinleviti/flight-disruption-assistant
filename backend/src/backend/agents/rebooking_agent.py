from backend.models.case_state import RebookingResult
from pathlib import Path
import litellm
from backend.tools.run_flight_query import RUN_FLIGHT_QUERY_TOOL, run_flight_query
import json
from pydantic import ValidationError

MODEL = "groq/openai/gpt-oss-120b",
FALLBACK_MODELS = [
    "gemini/gemini-3.5-flash-lite",
    "gemini/gemini-3.8-flash"
]
NUM_RETRIES = 0
MAX_TOOL_ROUNDS = 4  # safety limit: stop if the model keeps calling tools
PROMPT_PATH = Path(__file__).resolve().parent.parent / "prompts" / "rebooking_system.md"
REBOOKING_PROMPT = PROMPT_PATH.read_text(encoding="utf-8")
TOOL_SCHEMAS = [RUN_FLIGHT_QUERY_TOOL]  # the model sees these tools and can call them

TOOL_FUNCTIONS = {
    "run_flight_query": run_flight_query,
}

history = []

def run_tool(name: str, arguments_json: str) -> dict:
    function = TOOL_FUNCTIONS.get(name)
    if function is None:
        return {"error": f"Unknown tool '{name}'. Use only the tools you were given."}

    try:
        # It turns the model's arguments from text into a Python dictionary. from '{"booking_ref": "1254RF"}' to {"booking_ref": "1254RF"}
        arguments = json.loads(arguments_json or "{}")
    except json.JSONDecodeError:
        return {"error": "The tool arguments were not valid JSON."}

    try:
        # turns that dictionary into named arguments and calls the function. For example, if arguments is {"booking_ref": "1254RF"}, it will call get_booking(booking_ref="1254RF").
        return function(**arguments)
    except Exception as e:
        return {"error": f"Tool '{name}' failed: {e}"}

def get_flights_options(origin: str, destination: str, disrupted_flight_no: str, preferences: str = "", special_needs: str = "") -> dict:
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

    parameters_json = json.dumps(parameters, ensure_ascii=False)

    messages: list[dict] = [{"role": "system", "content": REBOOKING_PROMPT}]
    messages.append({"role": "user", "content": parameters_json})

    for _ in range(MAX_TOOL_ROUNDS):
        try:
            response = litellm.completion(
                model=MODEL,
                messages=messages,
                tools=TOOL_SCHEMAS,
                num_retries=NUM_RETRIES,
                fallbacks=FALLBACK_MODELS,
            )
        except litellm.BadRequestError as e:
            if "tool_use_failed" in str(e):
                print(f"unavailable tool attempted: {e}")
                messages.append({
                    "role": "system",
                    "content": "You tried to call a tool that isn't available. Use only the tools you were given"
                })
                continue
            raise
        reply = response.choices[0].message

        if not reply.tool_calls:
            try:
                answer = RebookingResult.model_validate_json(reply.content or "")
            except ValidationError as e:
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
            # A plain dict: the supervisor's json.dumps turns it into text for its model.
            return answer.model_dump(mode="json")

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
            result = run_tool(call.function.name, call.function.arguments)
            print(f"TOOL CALL: {call.function.name}({call.function.arguments}) -> {result}")
            messages.append({
                "role": "tool",
                "tool_call_id": call.id,
                "content": json.dumps(result)
            })
    return {"error": "I can't complete the request."}


REBOOKING_AGENT_TOOL = {
    "type": "function",
    "function": {
        "name": "rebooking_agent",
        "description": (
            "Specialist agent that searches the airline's flight inventory for alternatives to a "
            "disrupted flight and returns up to 3 options, a recommended flight and a short reason. "
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