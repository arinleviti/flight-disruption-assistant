from backend.models.case_state import (
    CaseState,
    Compensation,
    Disruption,
    FlightOption,
    OriginalBooking,
    Passenger,
)
from backend.tools.time_utils import to_local_time


def update_case_file(case: CaseState, tool_name: str, result: dict) -> None:
    """Save the useful facts from a tool's result into the case file.

    Called by code after every tool call, so the case file never depends on
    the model remembering anything. Results with an error change nothing.
    """
    if not isinstance(result, dict) or "error" in result:
        print(f"CASE FILE: nothing saved from {tool_name} (error or no result)")
        return

    if tool_name == "get_booking":
        #get_booking returns {"passenger": {...}, "booking": {...}}. These two lines take each half, turn it into
        # the matching model (Passenger, OriginalBooking), and put it into the case file's two empty slots.
        case.passenger = Passenger.model_validate(result["passenger"])
        case.original_booking = OriginalBooking.model_validate(result["booking"])
        print(
            f"CASE FILE: saved booking {case.original_booking.booking_ref} "
            f"({case.passenger.name}, {case.original_booking.flight_no})"
        )

    elif tool_name == "get_disruption":
        case.disruption = Disruption.model_validate(result)
        print(f"CASE FILE: saved disruption {case.disruption.flight_no} ({case.disruption.type})")

    elif tool_name == "rebooking_agent":
        # The latest search comes first, in the agent's ranking (best first).
        # Options from earlier searches that aren't in the new one are kept after them,
        # so the passenger can still pick one they saw before.
        latest = [FlightOption.model_validate(option) for option in result.get("options", [])]
        latest_ids = {option.flight_id for option in latest}
        older = [option for option in case.rebooking.options_offered if option.flight_id not in latest_ids]
        case.rebooking.options_offered = latest + older

        all_ids = [option.flight_id for option in case.rebooking.options_offered]
        print(f"CASE FILE: latest options {[o.flight_id for o in latest]}; all known: {all_ids}")

    elif tool_name == "record_rebooking":
        case.rebooking.confirmed = FlightOption.model_validate(result)
        print(
            f"CASE FILE: saved confirmed flight {case.rebooking.confirmed.flight_no} "
            f"({case.rebooking.confirmed.flight_id})"
        )

    elif tool_name == "compensation_agent":
        # The agent's reasoning and the calculator's rule are kept together,
        # so the case file says both why and on what basis
        case.compensation = Compensation(
            eligible=result["eligible"],
            amount_eur=result["amount_eur"],
            is_extraordinary=result["is_extraordinary"],
            reasoning=f"{result['reasoning']} {result['rule_applied']}",
            sources=result.get("sources", []),
        )
        print(
            f"CASE FILE: saved compensation {case.compensation.amount_eur} EUR "
            f"(extraordinary: {case.compensation.is_extraordinary})"
        )

    elif tool_name == "close_case":
        case.status = "closed"
        print(f"CASE FILE: case {case.case_id} closed")


def case_file_summary(case: CaseState) -> str:
    """Turn the case file into a short note the model reads at the start of every turn."""
    lines = [
        "CASE FILE: facts already recorded in this conversation. "
        "Use them instead of calling the same tools again."
    ]

    if case.status == "closed":
        lines.append(
            "Case status: CLOSED. Everything is settled. Answer the passenger's questions from "
            "the facts below; don't search, book or reassess anything."
        )
    else:
        lines.append(f"Case status: {case.status}")

    if case.original_booking and case.passenger:
        booking = case.original_booking
        lines.append(
            f"Booking: {booking.booking_ref}, passenger {case.passenger.name}, "
            f"flight {booking.flight_no} {booking.origin} -> {booking.destination}, "
            f"{booking.distance_km} km"
        )
        if case.passenger.special_needs:
            lines.append("Special needs: " + ", ".join(case.passenger.special_needs))
    else:
        lines.append("Booking: not identified yet")

    if case.disruption:
        disruption = case.disruption
        delay = ""
        if disruption.expected_delay_minutes:
            delay = f", expected delay {disruption.expected_delay_minutes} minutes"
        lines.append(
            f"Disruption: {disruption.flight_no} {disruption.type} "
            f"({disruption.stated_cause}){delay}"
        )
    else:
        lines.append("Disruption: not checked yet")

    if case.rebooking.options_offered:
        lines.append(
            "Flight options found (ranked best first). Show the passenger at most 3 at a time; "
            "if they ask for more, show the next ones from this list before searching again:"
        )
        for option in case.rebooking.options_offered:
            route = "direct" if option.via is None else f"via {option.via}"
            departs = to_local_time(option.departure, option.origin)
            arrives = to_local_time(option.arrival, option.destination)
            lines.append(
                f"- flight_id {option.flight_id}: {option.flight_no}, {route}, "
                f"departs {departs}, arrives {arrives} (local times)"
            )
    else:
        lines.append("Flight options found: none yet")

    confirmed = case.rebooking.confirmed
    if confirmed:
        departs = to_local_time(confirmed.departure, confirmed.origin)
        lines.append(
            f"Confirmed new flight: {confirmed.flight_no} (flight_id {confirmed.flight_id}), "
            f"departs {departs} local time. Already booked: do not book it again."
        )
    else:
        lines.append("Confirmed new flight: none yet")

    if case.compensation:
        compensation = case.compensation
        if compensation.eligible:
            outcome = f"owed {compensation.amount_eur} EUR"
        else:
            outcome = "no compensation owed"
        lines.append(
            f"Compensation: already assessed, {outcome} "
            f"(extraordinary circumstances: {'yes' if compensation.is_extraordinary else 'no'}). "
            f"Reason: {compensation.reasoning} Do not call compensation_agent again."
        )
    else:
        lines.append("Compensation: not assessed yet")

    return "\n".join(lines)