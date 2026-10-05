import json
from pathlib import Path

import litellm
from pydantic import BaseModel, ValidationError

from backend.models.case_state import CaseState
from backend.observability import call_llm, record_guard, trace_tool
from backend.tools.calculate_compensation import calculate_compensation
from backend.tools.search_regulations import SEARCH_REGULATIONS_TOOL, search_regulations

MODEL = "groq/openai/gpt-oss-120b"
FALLBACK_MODELS = [
    "gemini/gemini-3.5-flash-lite",
    "gemini/gemini-3.8-flash",
]
NUM_RETRIES = 0
MAX_TOOL_ROUNDS = 6  # searches and format retries both count as rounds

PROMPT_PATH = Path(__file__).resolve().parent.parent / "prompts" / "compensation_system.md"
COMPENSATION_PROMPT = PROMPT_PATH.read_text(encoding="utf-8")

# This agent's only tool: searching the legal texts. It never calculates amounts.
TOOL_SCHEMAS = [SEARCH_REGULATIONS_TOOL]
TOOL_FUNCTIONS = {
    "search_regulations": search_regulations,
}


class ExtraordinaryAssessment(BaseModel):
    """The agent's final answer: only its judgment, never an amount."""
    is_extraordinary: bool
    reasoning: str
    sources: list[str]


def run_tool(name: str, arguments_json: str) -> dict:
    # Same as the other agents' version, with this agent's TOOL_FUNCTIONS.
    function = TOOL_FUNCTIONS.get(name)
    if function is None:
        return {"error": f"Unknown tool '{name}'. Use only the tools you were given."}

    try:
        arguments = json.loads(arguments_json or "{}")
    except json.JSONDecodeError:
        return {"error": "The tool arguments were not valid JSON."}

    try:
        return function(**arguments)
    except Exception as e:
        return {"error": f"Tool '{name}' failed: {e}"}


def assess_extraordinary(case: CaseState) -> ExtraordinaryAssessment | dict:
    """The LLM part: search the legal texts and decide whether the cause is extraordinary.

    Returns the assessment, or an {"error": ...} dict if the agent couldn't finish.
    """
    booking = case.original_booking
    disruption = case.disruption

    # Only what the judgment needs: the facts about the cause, nothing about money
    request = {
        "flight_no": disruption.flight_no,
        "route": f"{booking.origin} -> {booking.destination}",
        "disruption_type": disruption.type,
        "stated_cause": disruption.stated_cause,
    }

    # A brand-new conversation: this agent never sees the passenger's chat
    messages: list[dict] = [
        {"role": "system", "content": COMPENSATION_PROMPT},
        {"role": "user", "content": json.dumps(request, ensure_ascii=False)},
    ]

    for _ in range(MAX_TOOL_ROUNDS):
        try:
            # Same as litellm.completion, but recorded in Langfuse and in the turn summary
            response = call_llm(
                agent="compensation",
                model=MODEL,
                messages=messages,
                tools=TOOL_SCHEMAS,
                fallbacks=FALLBACK_MODELS,
                num_retries=NUM_RETRIES,
            )
        except litellm.BadRequestError as e:
            if "tool_use_failed" in str(e):
                record_guard("compensation", "tool_not_available", str(e)[:300])
                messages.append({
                    "role": "system",
                    "content": "You tried to call a tool that isn't available. Use only the tools you were given.",
                })
                continue
            raise

        reply = response.choices[0].message

        # No tool requested: this should be the final JSON answer
        if not reply.tool_calls:
            try:
                return ExtraordinaryAssessment.model_validate_json(reply.content or "")
            except ValidationError as e:
                record_guard("compensation", "invalid_answer_format", f"{e.error_count()} problem(s) in the JSON answer")
                # Show the model its own reply and what's wrong with it, then let it try again
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

        # The model asked for tools. First, record its request...
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
        # ...then run each tool and add its result, linked by the call id
        for call in reply.tool_calls:
            # Runs the tool and records it (terminal, Langfuse, turn summary)
            result = trace_tool(
                "compensation",
                call.function.name,
                call.function.arguments,
                lambda call=call: run_tool(call.function.name, call.function.arguments),
            )
            messages.append({
                "role": "tool",
                "tool_call_id": call.id,
                "content": json.dumps(result, ensure_ascii=False),
            })

    record_guard("compensation", "max_rounds", f"no valid assessment after {MAX_TOOL_ROUNDS} rounds")
    return {"error": "The compensation assessment could not be completed. Escalate to a human colleague."}


def compensation_agent(case: CaseState) -> dict:
    """Decide whether compensation is owed, and how much.

    1. The agent (LLM + legal search) decides ONE thing: extraordinary or not.
    2. The code calculates the amount from that decision and the case file.
    The amount never passes through the model.
    """
    if case.original_booking is None or case.disruption is None:
        return {"error": "The booking and the disruption must be identified first."}

    # Cheap check first: no point running the agent if the amount can't be calculated yet
    if case.disruption.type == "cancellation" and case.rebooking.confirmed is None:
        return {
            "error": (
                "The new arrival time isn't known yet. Rebook the passenger first: "
                "compensation depends on how late they arrive."
            )
        }

    assessment = assess_extraordinary(case)
    if isinstance(assessment, dict):   # the agent couldn't finish
        return assessment

    # Code, not the model, turns the decision into an amount. Traced so the step is visible in Langfuse.
    calculation = trace_tool(
        "compensation",
        "calculate_compensation",
        {"booking_ref": case.case_id, "is_extraordinary": assessment.is_extraordinary},
        lambda: calculate_compensation(case, assessment.is_extraordinary),
    )
    if "error" in calculation:
        return calculation

    return {
        "eligible": calculation["eligible"],
        "amount_eur": calculation["amount_eur"],
        "is_extraordinary": assessment.is_extraordinary,
        "reasoning": assessment.reasoning,
        "rule_applied": calculation["rule_applied"],
        "sources": assessment.sources,
        "details": calculation["details"],
    }


COMPENSATION_AGENT_TOOL = {
    "type": "function",
    "function": {
        "name": "compensation_agent",
        "description": (
            "Specialist agent that decides whether the passenger is owed EU261 compensation for one "
            "booking, and how much. It reads that booking's disruption and confirmed new flight from "
            "the case file. Returns eligibility, the amount, the reasoning and the legal sources, or "
            "an error if the passenger hasn't been rebooked yet."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "booking_ref": {
                    "type": "string",
                    "description": "The booking reference of the case to assess, e.g. AZX4K2.",
                },
            },
            "required": ["booking_ref"],
        },
    },
}