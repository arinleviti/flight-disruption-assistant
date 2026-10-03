from datetime import timedelta

from backend.models.case_state import (
    CareEntitlements,
    CaseState,
    Compensation,
    Disruption,
    FlightOption,
    OriginalBooking,
    Passenger,
)
from backend.tools.time_utils import to_local_time


def normalize_ref(booking_ref: str) -> str:
    """Booking references are compared in one standard form: no spaces, uppercase."""
    return str(booking_ref or "").strip().upper()


def find_case_by_flight(cases: dict[str, CaseState], flight_no: str) -> CaseState | None:
    """The case whose original booking is on this flight number, if any."""
    flight_no = str(flight_no or "").strip().upper()
    for case in cases.values():
        if case.original_booking and case.original_booking.flight_no == flight_no:
            return case
    return None


def add_local_times(cases: dict[str, CaseState], result: dict) -> None:
    """Add local times to a get_disruption result, computed by code, before the model reads it.

    Adds the original flight's scheduled departure and arrival in local time, and for a
    delay, the expected new ones (scheduled time + delay). The model then copies these
    fields instead of doing time-zone arithmetic itself, which it gets wrong.
    """
    if not isinstance(result, dict) or "error" in result:
        return
    case = find_case_by_flight(cases, result.get("flight_no", ""))
    if case is None or case.original_booking is None:
        return

    booking = case.original_booking
    result["scheduled_departure_local"] = to_local_time(booking.scheduled_departure, booking.origin)
    result["scheduled_arrival_local"] = to_local_time(booking.scheduled_arrival, booking.destination)

    if result.get("type") == "delay" and result.get("expected_delay_minutes"):
        delay = timedelta(minutes=result["expected_delay_minutes"])
        result["expected_departure_local"] = to_local_time(booking.scheduled_departure + delay, booking.origin)
        result["expected_arrival_local"] = to_local_time(booking.scheduled_arrival + delay, booking.destination)


def update_case_file(cases: dict[str, CaseState], tool_name: str, arguments: dict | None, result: dict) -> None:
    """Save the useful facts from a tool's result into the right case.

    cases holds one case per booking reference. Each tool's result is routed
    to the case it belongs to, so several bookings never overwrite each other.
    Called by code after every tool call; results with an error change nothing.
    """
    if not isinstance(result, dict) or "error" in result:
        print(f"CASE FILE: nothing saved from {tool_name} (error or no result)")
        return
    arguments = arguments or {}

    if tool_name == "get_booking":
        # A new booking reference starts a new case; a known one updates its case
        ref = normalize_ref(result["booking"]["booking_ref"])
        case = cases.setdefault(ref, CaseState(case_id=ref))
        #get_booking returns {"passenger": {...}, "booking": {...}}. These two lines take each half, turn it into
        # the matching model (Passenger, OriginalBooking), and put it into the case file's two empty slots.
        case.passenger = Passenger.model_validate(result["passenger"])
        case.original_booking = OriginalBooking.model_validate(result["booking"])
        print(f"CASE FILE [{ref}]: saved booking ({case.passenger.name}, {case.original_booking.flight_no})")

    elif tool_name == "get_disruption":
        # A disruption belongs to the case whose booking is on that flight
        case = find_case_by_flight(cases, result["flight_no"])
        if case is None:
            print(f"CASE FILE: disruption for {result['flight_no']} matches no booking, not saved")
            return
        case.disruption = Disruption.model_validate(result)
        print(f"CASE FILE [{case.case_id}]: saved disruption {case.disruption.flight_no} ({case.disruption.type})")

    elif tool_name == "rebooking_agent":
        # Options belong to the case whose booking is on the disrupted flight
        case = find_case_by_flight(cases, arguments.get("disrupted_flight_no", ""))
        if case is None:
            print("CASE FILE: rebooking options match no booking, not saved")
            return
        # The latest search comes first, in the agent's ranking (best first).
        # Options from earlier searches that aren't in the new one are kept after them,
        # so the passenger can still pick one they saw before.
        latest = [FlightOption.model_validate(option) for option in result.get("options", [])]
        latest_ids = {option.flight_id for option in latest}
        older = [option for option in case.rebooking.options_offered if option.flight_id not in latest_ids]
        case.rebooking.options_offered = latest + older
        all_ids = [option.flight_id for option in case.rebooking.options_offered]
        print(f"CASE FILE [{case.case_id}]: latest options {[o.flight_id for o in latest]}; all known: {all_ids}")

    elif tool_name == "record_rebooking":
        case = cases.get(normalize_ref(result["booking_ref"]))
        if case is None:
            print(f"CASE FILE: rebooking for {result['booking_ref']} matches no case, not saved")
            return
        case.rebooking.confirmed = FlightOption.model_validate(result)
        print(f"CASE FILE [{case.case_id}]: saved confirmed flight {case.rebooking.confirmed.flight_no}")

    elif tool_name == "compute_care_entitlements":
        case = cases.get(normalize_ref(arguments.get("booking_ref", "")))
        if case is None:
            return
        vouchers = result["vouchers"]
        case.care.entitlements = CareEntitlements.model_validate(result["entitlements"])
        case.care.meal_vouchers = vouchers["meal"]["count"]
        case.care.meal_voucher_eur = vouchers["meal"]["amount_eur_each"]
        case.care.transport_voucher_eur = (
            vouchers["transport"]["amount_eur_each"] if vouchers["transport"]["count"] else 0
        )
        print(
            f"CASE FILE [{case.case_id}]: saved care ({case.care.meal_vouchers} meal vouchers, "
            f"{case.care.entitlements.hotel_nights} hotel nights)"
        )

    elif tool_name == "compensation_agent":
        case = cases.get(normalize_ref(arguments.get("booking_ref", "")))
        if case is None:
            return
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
            f"CASE FILE [{case.case_id}]: saved compensation {case.compensation.amount_eur} EUR "
            f"(extraordinary: {case.compensation.is_extraordinary})"
        )

    elif tool_name == "close_case":
        case = cases.get(normalize_ref(arguments.get("booking_ref", "")))
        if case is None:
            return
        case.status = "closed"
        print(f"CASE FILE [{case.case_id}]: case closed")


def describe_care(case: CaseState) -> str:
    """The care a passenger is owed, in one readable line (or 'nothing')."""
    entitlements = case.care.entitlements
    parts = []
    if entitlements.meals:
        parts.append(f"{case.care.meal_vouchers} meal voucher(s) of {case.care.meal_voucher_eur} EUR each")
    if entitlements.hotel_nights:
        parts.append(f"{entitlements.hotel_nights} hotel night(s)")
    if entitlements.transport:
        parts.append(f"transport between the airport and the hotel (voucher of {case.care.transport_voucher_eur} EUR)")
    if entitlements.communications:
        parts.append(f"{entitlements.communications} free calls or emails")
    return ", ".join(parts) if parts else "nothing (the wait is too short)"


def summarise_case(case: CaseState) -> list[str]:
    """The note lines for one case."""
    lines = []

    if case.status == "closed":
        lines.append(
            "Status: CLOSED. This booking is settled. Answer questions about it from the facts "
            "below; don't search, book or reassess anything for it."
        )
    else:
        lines.append(f"Status: {case.status}")

    booking = case.original_booking
    if booking and case.passenger:
        departs = to_local_time(booking.scheduled_departure, booking.origin)
        lines.append(
            f"Booking: passenger {case.passenger.name}, flight {booking.flight_no} "
            f"{booking.origin} -> {booking.destination}, {booking.distance_km} km, "
            f"originally scheduled to depart {departs} local time"
        )
        if case.passenger.special_needs:
            lines.append("Special needs: " + ", ".join(case.passenger.special_needs))

    if case.disruption:
        disruption = case.disruption
        delay = ""
        if disruption.type == "delay" and disruption.expected_delay_minutes and booking:
            shift = timedelta(minutes=disruption.expected_delay_minutes)
            new_departure = to_local_time(booking.scheduled_departure + shift, booking.origin)
            new_arrival = to_local_time(booking.scheduled_arrival + shift, booking.destination)
            delay = (
                f", expected delay {disruption.expected_delay_minutes} minutes: now expected to depart "
                f"{new_departure} and arrive {new_arrival} (local times)"
            )
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

    if case.care.entitlements:
        lines.append(
            f"Care: already worked out, owed {describe_care(case)}. Vouchers and hotel are collected "
            "at the Aurora Airways desk. Do not call compute_care_entitlements again for this booking."
        )
    else:
        lines.append("Care: not worked out yet")

    if case.compensation:
        compensation = case.compensation
        if compensation.eligible:
            outcome = f"owed {compensation.amount_eur} EUR"
        else:
            outcome = "no compensation owed"
        lines.append(
            f"Compensation: already assessed, {outcome} "
            f"(extraordinary circumstances: {'yes' if compensation.is_extraordinary else 'no'}). "
            f"Reason: {compensation.reasoning} Do not call compensation_agent again for this booking."
        )
    else:
        lines.append("Compensation: not assessed yet")

    return lines


def case_file_summary(cases: dict[str, CaseState]) -> str:
    """Turn all the cases into the short note the model reads at the start of every turn."""
    lines = [
        "CASE FILE: facts already recorded in this conversation, one case per booking reference. "
        "Use them instead of calling the same tools again. compensation_agent, "
        "compute_care_entitlements, close_case and record_rebooking need the booking reference "
        "of the case they act on."
    ]

    if not cases:
        lines.append("No booking identified yet.")
        return "\n".join(lines)

    for ref, case in cases.items():
        lines.append("")
        lines.append(f"=== Booking {ref} ===")
        lines.extend(summarise_case(case))

    return "\n".join(lines)