import sqlite3
from datetime import datetime, timezone

from backend.db.inventory import DB_PATH, SQLITE_DATE_FORMAT
from backend.tools.time_utils import to_local_time


def split_legs(flight_id: str) -> list[str]:
    """The single flights a trip is made of.

    search_flights packages a connection as "F-042+F-043", so a direct flight
    gives one id and a connection gives two, in the order they are flown.
    """
    return flight_id.split("+")


def local(utc_text: str, airport: str) -> str:
    """A UTC time from the database, as local time at an airport."""
    return to_local_time(datetime.strptime(utc_text, SQLITE_DATE_FORMAT), airport)


def build_confirmation(conn: sqlite3.Connection, booking_ref: str) -> dict:
    """Read the saved rebooking back from the database, with its flight details.

    The confirmation is built from what was actually saved, never from what
    the model thinks happened. For a connection, each flight is read on its own
    and the trip is put back together: from the first flight's origin to the
    last flight's destination, with the times at the connection airport.
    """
    saved = conn.execute(
        "SELECT booking_ref, booked_at, flight_id, flight_no FROM rebookings WHERE booking_ref = ?",
        [booking_ref],
    ).fetchone()

    legs = [
        conn.execute(
            "SELECT origin, destination, departure, arrival FROM flights WHERE flight_id = ?",
            [leg_id],
        ).fetchone()
        for leg_id in split_legs(saved["flight_id"])
    ]
    first, last = legs[0], legs[-1]   # the same flight when it's direct
    via = first["destination"] if len(legs) > 1 else None

    confirmation = {
        "booking_ref": saved["booking_ref"],
        "booked_at": saved["booked_at"],
        "flight_id": saved["flight_id"],
        "flight_no": saved["flight_no"],
        "origin": first["origin"],
        "destination": last["destination"],
        "via": via,
        "departure": first["departure"],
        "arrival": last["arrival"],
    }

    # Local times for the passenger, computed in code
    confirmation["departure_local"] = local(first["departure"], first["origin"])
    confirmation["arrival_local"] = local(last["arrival"], last["destination"])
    if via:
        confirmation["via_arrival"] = first["arrival"]
        confirmation["via_departure"] = last["departure"]
        confirmation["via_arrival_local"] = local(first["arrival"], via)
        confirmation["via_departure_local"] = local(last["departure"], via)
    confirmation["status"] = "confirmed"

    return confirmation


def record_rebooking(booking_ref: str, flight_id: str) -> dict:
    """Record the flight a passenger accepted: take one seat and save the rebooking.

    A connection needs a seat on each of its flights. Everything happens in one
    transaction: either every seat is taken AND the rebooking is saved, or nothing
    changes at all (never a seat on the first flight but not on the second).
    """
    booking_ref = booking_ref.strip().upper()
    flight_id = flight_id.strip().upper()
    if not booking_ref or not flight_id:
        return {"error": "booking_ref and flight_id are required."}

    leg_ids = split_legs(flight_id)

    # A normal connection: this tool has to write
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row

    try:
        # 1. Does this booking already have a rebooking?
        #execute() returns a cursor, fetchone() returns the first row or None if there are no rows.
        #[booking_ref] placed after the comma inside an execute call is a list of parameters to be substituted into the SQL query. The ? in the query is replaced by the value of booking_ref.
        existing = conn.execute(
            "SELECT flight_id FROM rebookings WHERE booking_ref = ?",
            [booking_ref],
        ).fetchone()

        # Same trip already recorded: change nothing, just confirm again.
        # This makes the tool safe to call twice (it happens).
        if existing is not None and existing["flight_id"] == flight_id:
            return build_confirmation(conn, booking_ref)

        # 2. Take one seat on every flight of the trip, but only if each is still bookable.
        # The check and the change are one statement, so two passengers
        # can never both take the last seat.
        for leg_id in leg_ids:
            cursor = conn.execute(
                """
                UPDATE flights
                SET seats_available = seats_available - 1
                WHERE flight_id = ?
                  AND seats_available > 0
                  AND departure > datetime('now')
                """,
                [leg_id],
            )

            # rowcount = how many rows the UPDATE changed: 1 means the seat was taken.
            # If one flight fails, the rollback also gives back the seats already taken on the others.
            if cursor.rowcount == 0:
                conn.rollback()
                return {
                    "error": (
                        f"Flight {flight_id} can't be booked: it's full, has already "
                        "departed, or doesn't exist. Search for alternatives again."
                    )
                }

        # 3. The passenger changed their mind: give the old seats back
        # and remove the old rebooking
        if existing is not None:
            for old_leg_id in split_legs(existing["flight_id"]):
                conn.execute(
                    "UPDATE flights SET seats_available = seats_available + 1 WHERE flight_id = ?",
                    [old_leg_id],
                )
            conn.execute(
                "DELETE FROM rebookings WHERE booking_ref = ?",
                [booking_ref],
            )

        # 4. Save the new rebooking, with the flight numbers of every flight ("AU406/AU435")
        flight_no = "/".join(
            conn.execute("SELECT flight_no FROM flights WHERE flight_id = ?", [leg_id]).fetchone()["flight_no"]
            for leg_id in leg_ids
        )

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
                    "description": "The flight_id of the accepted option from rebooking_agent, e.g. F-002, or F-042+F-043 for a connection.",
                },
            },
            "required": ["booking_ref", "flight_id"],
        },
    },
}
