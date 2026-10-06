"""Observability: what happened during one passenger turn.

Two outputs from the same recording:
1. Langfuse (full detail, kept forever): every LLM call with its exact messages, model, tokens
   and cost; every tool call with its arguments and result; every guard that fired. Nested,
   so the rebooking agent's searches appear inside the supervisor's rebooking_agent call.
2. TurnStats (a small summary for the chat UI): totals for this turn only, returned with the reply.

If the Langfuse keys are missing from .env, Langfuse simply records nothing and the app works
as before. TurnStats always works.
"""

import json
import time
from contextvars import ContextVar
from typing import Callable

import litellm
from langfuse import get_client

from backend.models.chat import GuardHit, ToolUse, TurnStats

langfuse = get_client()

# The current turn's summary. A ContextVar is a global that is private to each request:
# two passengers chatting at the same time each get their own TurnStats.
# _current_stats is a box that holds one thing at a time: a single TurnStats, or None before a turn starts.
# the box has 2 methods: get() returns the thing in the box, set(x) puts x in the box. The box is private to each request.
# one box per message, meaning one turn (the passenger's message plus the assistant's reply).
#And everything that runs during that turn uses the same box: every call_llm, every trace_tool (nested or not), every record_guard, in the supervisor and in both sub-agents.
#A new box only appears when the next message arrives: main.py calls start_turn() again, which puts a fresh, empty TurnStats in.
_current_stats: ContextVar[TurnStats | None] = ContextVar("current_stats", default=None)
_turn_started: ContextVar[float] = ContextVar("turn_started", default=0.0)

# Sub-agents run as tools of the supervisor, but Langfuse should show them as agents
AGENT_TOOLS = {"rebooking_agent", "compensation_agent"}

LOG_PREVIEW_CHARS = 300  # the terminal shows a short preview; Langfuse keeps the full result


def start_turn() -> TurnStats:
    """Start counting a new turn. Called once per /chat request, before answer_request."""
    stats = TurnStats()
    _current_stats.set(stats)
    _turn_started.set(time.perf_counter())
    return stats


def finish_turn() -> TurnStats | None:
    """Stop the clock and return this turn's summary."""
    stats = _current_stats.get()
    if stats is not None:
        stats.duration_ms = round((time.perf_counter() - _turn_started.get()) * 1000)
    return stats


def preview(value) -> str:
    """A short one-line version of a result, for the terminal."""
    text = json.dumps(value, ensure_ascii=False, default=str)
    return text if len(text) <= LOG_PREVIEW_CHARS else text[:LOG_PREVIEW_CHARS] + "…"


def response_cost(response) -> float:
    """The cost of one LLM call in USD, or 0.0 if litellm has no price for that model."""
    cost = (getattr(response, "_hidden_params", None) or {}).get("response_cost")
    if cost is None:
        try:
            cost = litellm.completion_cost(completion_response=response)
        except Exception:
            cost = 0.0
    return float(cost or 0.0)


def reply_for_trace(message) -> dict:
    """The model's reply as plain data: its text and any tool calls it asked for."""
    return {
        "content": message.content,
        "tool_calls": [
            {"name": call.function.name, "arguments": call.function.arguments}
            for call in (message.tool_calls or [])
        ],
    }


def call_llm(agent: str, model: str, messages: list[dict], tools: list[dict], fallbacks: list[str], num_retries: int):
    """litellm.completion, recorded.

    Every agent calls the model through here, so every LLM call is traced the same way:
    which model actually answered (fallbacks included), tokens, cost and latency.
    Exceptions are recorded and raised again, so each agent's own error handling still works.
    """
    #here langfuse opens a new trace. Generation is called like that because it is a generation of text.
    with langfuse.start_as_current_observation(
        name=f"{agent}.llm",
        as_type="generation",
        input=messages,
        metadata={"agent": agent, "requested_model": model},
    ) as generation:
        try:
            response = litellm.completion(
                model=model,
                messages=messages,
                tools=tools,
                num_retries=num_retries,
                fallbacks=fallbacks,
            )
        except Exception as e:
            generation.update(level="ERROR", status_message=f"{type(e).__name__}: {e}")
            raise

        served_model = response.model or model
        #give me response.usage, but if response has no usage, give me None instead of crashing
        usage = getattr(response, "usage", None)
        input_tokens = getattr(usage, "prompt_tokens", 0) or 0
        output_tokens = getattr(usage, "completion_tokens", 0) or 0
        cost = response_cost(response)
        # The model we asked for, without its provider prefix, e.g. "gpt-oss-120b"
        fell_back = model.split("/")[-1] not in served_model
        #here update hands off the info to langfuse.
        generation.update(
            model=served_model,
            output=reply_for_trace(response.choices[0].message),
            usage_details={"input": input_tokens, "output": output_tokens},
            cost_details={"total": cost} if cost else None,
            level="WARNING" if fell_back else None,
            status_message=f"fallback: {model} failed, answered by {served_model}" if fell_back else None,
        )

    stats = _current_stats.get()
    if stats is not None:
        stats.llm_calls += 1
        stats.input_tokens += input_tokens
        stats.output_tokens += output_tokens
        stats.cost_usd = round(stats.cost_usd + cost, 6)
        if served_model not in stats.models:
            stats.models.append(served_model)
        if fell_back:
            stats.fallbacks += 1

    return response

# run: Callable[[], dict] a function that takes nothing and returns a dict
def trace_tool(agent: str, name: str, arguments, run: Callable[[], dict]) -> dict:
    """Run one tool call, recorded.

    `run` is the actual call, e.g. lambda: run_tool(name, arguments, cases).
    The arguments are recorded again after the call, because code may have added to them
    (e.g. original_departure_local, or a booking_ref filled in by a guard).
    """
    started = time.perf_counter()
    as_type = "agent" if name in AGENT_TOOLS else "tool"

    # Add the tool to the turn summary NOW, when it starts, so the list is in the order the
    # tools began (a sub-agent comes before the tools it calls). Its result is filled in at the end.
    tool_use = ToolUse(agent=agent, name=name, ok=True, duration_ms=0)
    stats = _current_stats.get()
    if stats is not None:
        stats.tools.append(tool_use)

    # Sub-agents pass the model's raw JSON text: show it as data, not as one long string
    if isinstance(arguments, str):
        try:
            arguments = json.loads(arguments or "{}")
        except json.JSONDecodeError:
            pass

    with langfuse.start_as_current_observation(
        name=name,
        as_type=as_type,
        input=arguments,
        metadata={"called_by": agent},
    ) as observation:
        result = run()
        error = result.get("error") if isinstance(result, dict) else None
        observation.update(
            input=arguments,
            output=result,
            level="WARNING" if error else None,
            status_message=error,
        )

    # Now the tool has finished: fill in how it went
    tool_use.ok = error is None
    tool_use.duration_ms = round((time.perf_counter() - started) * 1000)
    print(f"TOOL CALL [{agent}]: {name}({preview(arguments)}) -> {preview(result)}")

    return result


def record_guard(agent: str, name: str, detail: str) -> None:
    """A guard fired: code corrected or refused something the model did.

    Shows up in the terminal, in Langfuse (as a guardrail step, inside whatever was running)
    and in the turn summary.
    """
    print(f"GUARD [{agent}] {name}: {detail}")
    langfuse.start_observation(
        name=name,
        as_type="guardrail",
        input={"agent": agent},
        output=detail,
        level="WARNING",
    ).end()

    stats = _current_stats.get()
    if stats is not None:
        stats.guards.append(GuardHit(agent=agent, name=name, detail=detail))