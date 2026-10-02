import sqlite3
from datetime import datetime, timezone

from backend.db.inventory import DB_PATH, SQLITE_DATE_FORMAT
from backend.tools.time_utils import to_local_time


def build_confirmation(conn: sqlite3.Connection, booking_ref: str) -> dict:
    """Read the saved rebooking back from the database, with its flight details.

    The confirmation is built from what was actually saved, never from what
    the model thinks happened.
    """
    row = conn.execute(
        """
        SELECT rebookings.booking_ref, rebookings.booked_at,
               flights.flight_id, flights.flight_no, flights.origin, flights.destination,
               flights.via, flights.departure, flights.arrival
        FROM rebookings
        JOIN flights ON flights.flight_id = rebookings.flight_id
        WHERE rebookings.booking_ref = ?
        """,
        [booking_ref],
    ).fetchone()

    confirmation = dict(row)

    # Local times for the passenger, computed in code
    departure = datetime.strptime(confirmation["departure"], SQLITE_DATE_FORMAT)
    arrival = datetime.strptime(confirmation["arrival"], SQLITE_DATE_FORMAT)
    confirmation["departure_local"] = to_local_time(departure, confirmation["origin"])
    confirmation["arrival_local"] = to_local_time(arrival, confirmation["destination"])
    confirmation["status"] = "confirmed"

    return confirmation


def record_rebooking(booking_ref: str, flight_id: str) -> dict:
    """Record the flight a passenger accepted: take one seat and save the rebooking.

    Everything happens in one transaction: either the seat is taken AND the
    rebooking is saved, or nothing changes at all.
    """
    booking_ref = booking_ref.strip().upper()
    flight_id = flight_id.strip().upper()
    if not booking_ref or not flight_id:
        return {"error": "booking_ref and flight_id are required."}

    # A normal connection: this tool has to write
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row

    try:
        # 1. Does this booking already have a rebooking?
        existing = conn.execute(
            "SELECT flight_id FROM rebookings WHERE booking_ref = ?",
            [booking_ref],
        ).fetchone()

        # Same flight already recorded: change nothing, just confirm again.
        # This makes the tool safe to call twice (it happens).
        if existing is not None and existing["flight_id"] == flight_id:
            return build_confirmation(conn, booking_ref)

        # 2. Take one seat, but only if the flight is still bookable.
        # The check and the change are one statement, so two passengers
        # can never both take the last seat.
        cursor = conn.execute(
            """
            UPDATE flights
            SET seats_available = seats_available - 1
            WHERE flight_id = ?
              AND seats_available > 0
              AND departure > datetime('now')
            """,
            [flight_id],
        )

        # rowcount = how many rows the UPDATE changed: 1 means the seat was taken
        if cursor.rowcount == 0:
            conn.rollback()
            return {
                "error": (
                    f"Flight {flight_id} can't be booked: it's full, has already "
                    "departed, or doesn't exist. Search for alternatives again."
                )
            }

        # 3. The passenger changed their mind: give the old seat back
        # and remove the old rebooking
        if existing is not None:
            conn.execute(
                "UPDATE flights SET seats_available = seats_available + 1 WHERE flight_id = ?",
                [existing["flight_id"]],
            )
            conn.execute(
                "DELETE FROM rebookings WHERE booking_ref = ?",
                [booking_ref],
            )

        # 4. Save the new rebooking
        flight_no = conn.execute(
            "SELECT flight_no FROM flights WHERE flight_id = ?",
            [flight_id],
        ).fetchone()["flight_no"]

        booked_at = datetime.now(timezone.utc).strftime(SQLITE_DATE_FORMAT)

        conn.execute(
            "INSERT INTO rebookings VALUES (?, ?, ?, ?)",
            [booking_ref, flight_id, flight_no, booked_at],
        )

        # 5. Save everything at once
        conn.commit()

        return build_confirmation(conn, booking_ref)

    except sqlite3.Error as e:
        # Something failed halfway: undo every change made in this transaction
        conn.rollback()
        return {"error": f"The rebooking could not be saved: {e}"}

    finally:
        conn.close()


RECORD_REBOOKING_TOOL = {
    "type": "function",
    "function": {
        "name": "record_rebooking",
        "description": (
            "Records the replacement flight a passenger has accepted: takes one seat on that "
            "flight and saves the rebooking. Returns the confirmed booking with local times, "
            "or an error if the flight is no longer available. Only for flights the passenger "
            "has explicitly accepted."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "booking_ref": {
                    "type": "string",
                    "description": "The passenger's booking reference, as returned by get_booking.",
                },
                "flight_id": {
                    "type": "string",
                    "description": "The flight_id of the accepted option from rebooking_agent, e.g. F-002.",
                },
            },
            "required": ["booking_ref", "flight_id"],
        },
    },
}