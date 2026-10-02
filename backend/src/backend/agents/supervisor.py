from backend.models.chat import Message
from backend.models.case_state import CaseState
from pathlib import Path
import litellm
from backend.tools.get_booking import GET_BOOKING_TOOL, get_booking
from backend.tools.get_disruption import GET_DISRUPTION_TOOL, get_disruption
from backend.agents.rebooking_agent import REBOOKING_AGENT_TOOL, get_flights_options
from backend.tools.record_rebooking import RECORD_REBOOKING_TOOL, record_rebooking
import json
from backend.state.case_file import case_file_summary, update_case_file

MODEL = "groq/openai/gpt-oss-120b"
FALLBACK_MODELS = [
    "gemini/gemini-3.5-flash-lite",
    "gemini/gemini-3.8-flash"
]
NUM_RETRIES = 0
MAX_TOOL_ROUNDS = 8  # safety limit: stop if the model keeps calling tools
PROMPT_PATH = Path(__file__).resolve().parent.parent / "prompts" / "supervisor_system.md"
SUPERVISOR_PROMPT = PROMPT_PATH.read_text(encoding="utf-8")
TOOL_SCHEMAS = [GET_BOOKING_TOOL, GET_DISRUPTION_TOOL, REBOOKING_AGENT_TOOL, RECORD_REBOOKING_TOOL]  # the model sees these tools and can call them

# Keys must match the "name" in each tool schema; values are the Python functions to run.
TOOL_FUNCTIONS = {
    "get_booking": get_booking,
    "get_disruption": get_disruption,
    "rebooking_agent": get_flights_options,
    "record_rebooking": record_rebooking,
}

def run_tool(name: str, arguments_json: str, case: CaseState) -> dict:
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

def answer_request(message: str, history: list[Message] | None = None, case: CaseState | None = None) -> str:

    if history is None:
        history = []

    if case is None:
        case = CaseState(case_id="no-session")

    messages: list[dict] = [{"role": "system", "content": SUPERVISOR_PROMPT}]
    # The case file: facts from earlier tool results, which the history doesn't contain
    messages.append({"role": "system", "content": case_file_summary(case)})
    messages.extend([m.model_dump() for m in history])

    messages.append({"role": "user", "content": message})

    #range() generates a sequence of numbers for a loop.
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
            # Groq rejects calls to tools that weren't sent in TOOL_SCHEMAS.
            # Tell the model and let it try again, instead of crashing.
            if "tool_use_failed" in str(e):
                print(f"UNAVAILABLE TOOL ATTEMPTED: {e}")
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
        reply = response.choices[0].message

        # No tool requested: the model has answered, we're done
        if not reply.tool_calls:
            return reply.content or ""

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
        # ...then run each tool, save its facts to the case file, and add its result
        for call in reply.tool_calls:
            result = run_tool(call.function.name, call.function.arguments, case)
            update_case_file(case, call.function.name, result)
            print(f"TOOL CALL: {call.function.name}({call.function.arguments}) -> {result}")
            messages.append({
                "role": "tool",
                "tool_call_id": call.id,
                "content": json.dumps(result, ensure_ascii=False)  #here we turn the Py dictionary result into a string, so the model can read it. For example, {"error": "The flight number is empty."} becomes '{"error": "The flight number is empty."}'
            })

        # Loop back: the model now sees the results and continues

    return "I'm sorry, I'm having trouble completing this right now. Let me connect you with a colleague."