import sqlite3
from datetime import timedelta

from backend.db.inventory import DB_PATH
from backend.models.case_state import CaseState
from backend.tools.record_rebooking import build_confirmation
from backend.tools.time_utils import to_local_time


def read_recorded_rebooking(booking_ref: str) -> dict | None:
    """The rebooking exactly as saved in the database, or None if there isn't one.

    Read-only: closing a case never changes the flights or the rebookings.
    """
    conn = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        exists = conn.execute(
            "SELECT 1 FROM rebookings WHERE booking_ref = ?", [booking_ref]
        ).fetchone()
        if exists is None:
            return None
        # Same helper record_rebooking uses: the rebooking joined with its flight, plus local times
        return build_confirmation(conn, booking_ref)
    finally:
        conn.close()


def close_case(case: CaseState) -> dict:
    """Check the case is complete, and return the final summary built from the records.

    The flight comes from the database (what was actually booked), the compensation
    from the case file. Refuses to close if a step is missing, and says which one.
    """
    booking = case.original_booking
    disruption = case.disruption
    if booking is None or disruption is None:
        return {"error": "The booking and the disruption must be identified before closing the case."}

    recorded = read_recorded_rebooking(booking.booking_ref)

    # A cancelled flight must be replaced before the case can close
    if disruption.type == "cancellation" and recorded is None:
        return {"error": "The passenger has no confirmed new flight. Rebook them before closing the case."}

    if case.compensation is None:
        return {"error": "Compensation hasn't been assessed yet. Call compensation_agent before closing the case."}

    if recorded is not None:
        # The new flight, exactly as recorded in the database
        flight = {
            "flight_no": recorded["flight_no"],
            "origin": recorded["origin"],
            "destination": recorded["destination"],
            "via": recorded["via"],
            "departure_local": recorded["departure_local"],
            "arrival_local": recorded["arrival_local"],
        }
        keeps_original_flight = False
    else:
        # A delayed passenger who kept their flight: the original flight, shifted by the delay
        delay = timedelta(minutes=disruption.expected_delay_minutes or 0)
        flight = {
            "flight_no": booking.flight_no,
            "origin": booking.origin,
            "destination": booking.destination,
            "via": None,
            "departure_local": to_local_time(booking.scheduled_departure + delay, booking.origin),
            "arrival_local": to_local_time(booking.scheduled_arrival + delay, booking.destination),
        }
        keeps_original_flight = True

    return {
        "status": "closed",
        "booking_ref": booking.booking_ref,
        "passenger": case.passenger.name if case.passenger else None,
        "keeps_original_flight": keeps_original_flight,
        "flight": flight,
        "compensation": {
            "eligible": case.compensation.eligible,
            "amount_eur": case.compensation.amount_eur,
            "reason": case.compensation.reasoning,
        },
    }


CLOSE_CASE_TOOL = {
    "type": "function",
    "function": {
        "name": "close_case",
        "description": (
            "Closes one booking's case once its flight is settled and compensation has been assessed. "
            "Reads everything from that case and the booking records. Returns the final summary "
            "(flight with local times, compensation), or an error naming the step that is still missing."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "booking_ref": {
                    "type": "string",
                    "description": "The booking reference of the case to close, e.g. AZX4K2.",
                },
            },
            "required": ["booking_ref"],
        },
    },
}