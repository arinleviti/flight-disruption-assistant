from backend.models.case_state import (
    CaseState,
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
        # Keep every option ever offered, without duplicates:
        # the passenger may pick one from an earlier search
        known_ids = {option.flight_id for option in case.rebooking.options_offered}
        added = []
        for option in result.get("options", []):
            if option["flight_id"] not in known_ids:
                case.rebooking.options_offered.append(FlightOption.model_validate(option))
                known_ids.add(option["flight_id"])
                added.append(option["flight_id"])
        all_ids = [option.flight_id for option in case.rebooking.options_offered]
        print(f"CASE FILE: added options {added or 'none (already known)'}; all offered: {all_ids}")

    elif tool_name == "record_rebooking":
        case.rebooking.confirmed = FlightOption.model_validate(result)
        print(
            f"CASE FILE: saved confirmed flight {case.rebooking.confirmed.flight_no} "
            f"({case.rebooking.confirmed.flight_id})"
        )


def case_file_summary(case: CaseState) -> str:
    """Turn the case file into a short note the model reads at the start of every turn."""
    lines = [
        "CASE FILE: facts already recorded in this conversation. "
        "Use them instead of calling the same tools again."
    ]

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
        lines.append("Flight options offered to the passenger:")
        for option in case.rebooking.options_offered:
            route = "direct" if option.via is None else f"via {option.via}"
            departs = to_local_time(option.departure, option.origin)
            arrives = to_local_time(option.arrival, option.destination)
            lines.append(
                f"- flight_id {option.flight_id}: {option.flight_no}, {route}, "
                f"departs {departs}, arrives {arrives} (local times)"
            )
    else:
        lines.append("Flight options offered: none yet")

    confirmed = case.rebooking.confirmed
    if confirmed:
        departs = to_local_time(confirmed.departure, confirmed.origin)
        lines.append(
            f"Confirmed new flight: {confirmed.flight_no} (flight_id {confirmed.flight_id}), "
            f"departs {departs} local time. Already booked: do not book it again."
        )
    else:
        lines.append("Confirmed new flight: none yet")

    return "\n".join(lines)