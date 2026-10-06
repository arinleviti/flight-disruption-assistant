"""Run the eval scenarios and check the results in code.

For each demo passenger: start a fresh conversation, send the scripted messages one by one
through the real answer_request (the same function the /chat endpoint uses), then check the
case file against what the scenario expects.

Run it from the backend folder (the server doesn't need to be running):
    uv run python -m backend.evals.run_evals
    uv run python -m backend.evals.run_evals --only TMQ4L9 KMW3P8

Every turn is also traced in Langfuse, tagged "eval", with one session per scenario.
A full report (checks, stats, the whole conversation) is saved to eval_results/<date-time>.json.
"""
#uv run python -m backend.evals.run_evals                  # all 9 passengers
#uv run python -m backend.evals.run_evals --only TMQ4L9     # just one (or several) --only TMQ4L9 KMW3P8, etc.
from dotenv import load_dotenv

load_dotenv()  # before importing anything that reads environment variables (API keys, Langfuse keys)

import argparse
import json
import time
from datetime import datetime
from pathlib import Path

from langfuse import propagate_attributes

from backend.agents.supervisor import FLIGHT_NUMBER, answer_request, known_flight_numbers
from backend.db.inventory import build_inventory_db
from backend.evals.scenarios import SCENARIOS, Scenario
from backend.models.case_state import CaseState
from backend.models.chat import Message, TurnStats
from backend.observability import finish_turn, langfuse, start_turn
from backend.rag.knowledge_base import build_knowledge_base
from backend.tools.time_utils import to_local_time

PAUSE_BETWEEN_TURNS = 2   # seconds: gives the free-tier rate limits a little room
RESULTS_DIR = Path("eval_results")


# ---------------------------------------------------------------- running one scenario

def run_scenario(scenario: Scenario, run_id: str) -> dict:
    """Play one scripted passenger through the assistant. Returns the conversation and the case file."""
    history: list[Message] = []
    cases: dict[str, CaseState] = {}     # a fresh, empty case file, like a new browser session
    turns: list[dict] = []
    session_id = f"eval-{run_id}-{scenario.ref}"

    # A clean flight inventory for every scenario: earlier scenarios' bookings are wiped
    build_inventory_db()

    for message in scenario.messages:
        start_turn()
        # Same tracing as main.py, so every eval turn appears in Langfuse
        with langfuse.start_as_current_observation(
            name="eval_turn",
            as_type="agent",
            input={"message": message, "scenario": scenario.ref},
        ) as turn:
            with propagate_attributes(session_id=session_id, trace_name="eval_turn", tags=["eval"]):
                reply = answer_request(message, history, cases)
            turn.update(output=reply)
        stats = finish_turn()

        history.append(Message(role="user", content=message))
        history.append(Message(role="assistant", content=reply))
        turns.append({"passenger": message, "assistant": reply, "stats": stats.model_dump() if stats else None})

        print(f"\n  PASSENGER: {message}\n  ASSISTANT: {reply}")
        time.sleep(PAUSE_BETWEEN_TURNS)

    return {"turns": turns, "cases": cases}


# ---------------------------------------------------------------- checking the result
#builds one check result as a dictionary.
def check(name: str, passed: bool, detail: str = "") -> dict:
    return {"check": name, "passed": bool(passed), "detail": detail}


def tools_that_succeeded(turns: list[dict]) -> list[str]:
    """The names of every supervisor tool call that worked, across the whole conversation."""
    names = []
    for turn in turns:
        for tool in (turn["stats"] or {}).get("tools", []):
            if tool["agent"] == "supervisor" and tool["ok"]:
                names.append(tool["name"])
    return names

#answers one question: "At the end of this conversation, is everything the way it should be?" It never reads the assistant's wording. It looks at data.
#What it receives (3 things):

#scenario: what should have happened, from scenarios.py (for example rebooked=True, is_extraordinary=False, amounts_eur={0})
#cases: the case file at the end of the conversation, which is what actually happened
#turns: every message and reply, plus the stats for each turn (which tools ran, which guards fired)
def run_checks(scenario: Scenario, turns: list[dict], cases: dict[str, CaseState]) -> list[dict]:
    """Compare the end of the conversation with what the scenario expects."""
    results = []
    case = cases.get(scenario.ref)
    succeeded = tools_that_succeeded(turns)

    # 1. The booking was found
    #
    results.append(check("booking loaded", case is not None and case.passenger is not None))
    if case is None:
        return results   # nothing else can be checked

    # 2. The disruption was found (or correctly not found)
    if scenario.has_disruption:
        results.append(check("disruption loaded", case.disruption is not None))
    else:
        results.append(check("no disruption, nothing rebooked or assessed",
                             case.disruption is None and "rebooking_agent" not in succeeded
                             and "compensation_agent" not in succeeded))

    # 3. Rebooked, or kept the original flight
    confirmed = case.rebooking.confirmed
    if scenario.rebooked is True:
        results.append(check("new flight booked", confirmed is not None,
                             confirmed.flight_no if confirmed else "no booking"))
        results.append(check("booked exactly once", succeeded.count("record_rebooking") == 1,
                             f"record_rebooking succeeded {succeeded.count('record_rebooking')} time(s)"))
    elif scenario.rebooked is False:
        results.append(check("original flight kept", confirmed is None and "record_rebooking" not in succeeded))

    # 4. Rules about the booked flight
    if scenario.direct_only and confirmed:
        results.append(check("booked flight is direct", not confirmed.via, f"via {confirmed.via}" if confirmed.via else "direct"))
    if scenario.not_before_original_date and confirmed and case.original_booking:
        original = case.original_booking
        original_date = to_local_time(original.scheduled_departure, original.origin)[:10]
        booked_date = to_local_time(confirmed.departure, confirmed.origin)[:10]
        results.append(check("not before the original date", booked_date >= original_date,
                             f"original {original_date}, booked {booked_date}"))

    # 5. Care
    if scenario.care_expected:
        results.append(check("care worked out", "compute_care_entitlements" in succeeded))

    # 6. Compensation: the legal judgment, eligibility and amount
    compensation = case.compensation
    if scenario.is_extraordinary is not None:
        results.append(check("compensation assessed", compensation is not None))
        if compensation is not None:
            results.append(check("extraordinary judgment", compensation.is_extraordinary == scenario.is_extraordinary,
                                 f"expected {scenario.is_extraordinary}, got {compensation.is_extraordinary}"))
            if scenario.eligible is not None:
                results.append(check("eligibility", compensation.eligible == scenario.eligible,
                                     f"expected {scenario.eligible}, got {compensation.eligible}"))
            if scenario.amounts_eur:
                results.append(check("amount", compensation.amount_eur in scenario.amounts_eur,
                                     f"expected one of {sorted(scenario.amounts_eur)}, got {compensation.amount_eur}"))

    # 7. Closed
    if scenario.closed:
        results.append(check("case closed", case.status == "closed", f"status: {case.status}"))

    # 8. No reply mentioned a flight that doesn't exist
    known = known_flight_numbers(cases)
    invented = sorted({n for turn in turns for n in FLIGHT_NUMBER.findall(turn["assistant"])} - known)
    results.append(check("no invented flights in replies", not invented, ", ".join(invented)))

    return results


# ---------------------------------------------------------------- the whole run

def totals(turns: list[dict]) -> dict:
    """Add up the stats of every turn of one scenario."""
    all_stats = [TurnStats.model_validate(t["stats"]) for t in turns if t["stats"]]
    guards: dict[str, int] = {}
    for stats in all_stats:
        for guard in stats.guards:
            guards[guard.name] = guards.get(guard.name, 0) + 1
    return {
        "seconds": round(sum(s.duration_ms for s in all_stats) / 1000, 1),
        "llm_calls": sum(s.llm_calls for s in all_stats),
        "tokens": sum(s.input_tokens + s.output_tokens for s in all_stats),
        "cost_usd": round(sum(s.cost_usd for s in all_stats), 4),
        "fallbacks": sum(s.fallbacks for s in all_stats),
        "models": sorted({m for s in all_stats for m in s.models}),
        "guards": guards,
    }


def main():
    #argparse argparse is Python's built-in tool for reading options typed in the terminal after the command.
    #creates a "parser" object that will read them. description is the text shown if you run the script with --help
    parser = argparse.ArgumentParser(description="Run the eval scenarios.")
    #nargs="*" means "any number of values after it", so --only TMQ4L9 KMW3P8 gives a list: ["TMQ4L9", "KMW3P8"]
    parser.add_argument("--only", nargs="*", help="booking references to run (default: all)")
    #reads what i typed in the terminal and turns it into a dictionary of options, like {"only": ["TMQ4L9", "KMW3P8"]}
    args = parser.parse_args()

    # Which passengers to run: the ones typed after --only, or all of them
    if args.only:
        wanted = [ref.upper() for ref in args.only]
        scenarios = [scenario for scenario in SCENARIOS if scenario.ref in wanted]
    else:
        scenarios = SCENARIOS
    run_id = datetime.now().strftime("%Y%m%d-%H%M")
    build_knowledge_base()

    report = []
    for scenario in scenarios:
        print(f"\n=== {scenario.ref} · {scenario.title} ===")
        played = run_scenario(scenario, run_id)
        results = run_checks(scenario, played["turns"], played["cases"])
        report.append({
            "ref": scenario.ref,
            "title": scenario.title,
            "checks": results,
            "totals": totals(played["turns"]),
            "turns": played["turns"],
        })

    langfuse.flush()  # send the last traces before the script ends

    # ---- summary in the terminal
    print("\n\n================ EVAL RESULTS ================")
    all_checks = 0
    all_passed = 0
    for entry in report:
        passed = sum(c["passed"] for c in entry["checks"])
        total = len(entry["checks"])
        all_checks += total
        all_passed += passed
        t = entry["totals"]
        mark = "PASS" if passed == total else "FAIL"
        print(f"\n{mark}  {entry['ref']} · {entry['title']}  ({passed}/{total} checks, "
              f"{t['seconds']} s, {t['llm_calls']} LLM calls, {t['tokens']:,} tokens, ${t['cost_usd']})")
        for c in entry["checks"]:
            if not c["passed"]:
                print(f"      ✗ {c['check']}: {c['detail']}")
        if t["guards"]:
            print(f"      guards fired: {', '.join(f'{name} ×{n}' for name, n in t['guards'].items())}")

    scenarios_passed = sum(all(c["passed"] for c in e["checks"]) for e in report)
    print(f"\nScenarios fully passed: {scenarios_passed}/{len(report)}")
    print(f"Checks passed: {all_passed}/{all_checks}")
    print(f"Total cost: ${round(sum(e['totals']['cost_usd'] for e in report), 4)}")

    # ---- full report on disk
    RESULTS_DIR.mkdir(exist_ok=True)
    path = RESULTS_DIR / f"{run_id}.json"
    path.write_text(json.dumps(report, indent=2, ensure_ascii=False, default=str), encoding="utf-8")
    print(f"Full report: {path}")


if __name__ == "__main__":
    main()