import sqlite3
from datetime import datetime

from backend.db.inventory import DB_PATH
from backend.tools.time_utils import DATABASE_FORMAT, local_to_utc, to_local_time

MAX_ROWS = 20  # never return more than this

# A connection needs time to change planes, but not a whole day at the airport
MIN_CONNECTION_MINUTES = 45
MAX_CONNECTION_MINUTES = 6 * 60

# Direct flights: one row of the table is one trip. "a" is a nickname for the table.
# NULL fills the connection columns, so both queries return the same columns (UNION ALL needs that).
DIRECT_SQL = """
    SELECT a.flight_id, a.flight_no, a.origin, a.destination, NULL AS via,
           a.departure, a.arrival, NULL AS via_arrival, NULL AS via_departure,
           a.seats_available
    FROM available_flights a
    WHERE a.origin = ? AND a.destination = ?
"""

# Connections: the same table twice, as "a" (first flight) and "b" (second flight).
# JOIN ... ON keeps only the pairs where the first flight lands at the airport the second one leaves from,
# with enough time to change planes in between. The database builds the pairs, not Python.
CONNECTION_SQL = """
    SELECT a.flight_id || '+' || b.flight_id AS flight_id,
           a.flight_no || '/' || b.flight_no AS flight_no,
           a.origin AS origin, b.destination AS destination, a.destination AS via,
           a.departure AS departure, b.arrival AS arrival,
           a.arrival AS via_arrival, b.departure AS via_departure,
           MIN(a.seats_available, b.seats_available) AS seats_available
    FROM available_flights a
    JOIN available_flights b
      ON a.destination = b.origin
     AND b.departure >= datetime(a.arrival, ?)
     AND b.departure <= datetime(a.arrival, ?)
    WHERE a.origin = ? AND b.destination = ?
"""


def trip_filters(first: str, last: str, exclude_flight_no: str, avoid: list[str],
                 after_utc: str, before_utc: str, arrive_before_utc: str) -> tuple[list[str], list]:
    """The passenger's filters as extra SQL conditions, for a direct flight or a connection.

    first and last are the nicknames of the trip's first and last flight: both "a" for a direct
    flight, "a" and "b" for a connection. Departure filters apply to the first flight, the
    arrival filter to the last one. Every value goes in a ? placeholder.
    """
    conditions, values = [], []

    if exclude_flight_no:
        # dict.fromkeys removes the duplicate "a" for direct flights, keeping the order
        for alias in dict.fromkeys([first, last]):
            conditions.append(f"{alias}.flight_no != ?")
            values.append(exclude_flight_no)

    # Only a connection has a connection airport to avoid
    if first != last:
        for airport in avoid:
            conditions.append(f"{first}.destination != ?")
            values.append(airport)

    if after_utc:
        conditions.append(f"{first}.departure >= ?")
        values.append(after_utc)
    if before_utc:
        conditions.append(f"{first}.departure <= ?")
        values.append(before_utc)
    if arrive_before_utc:
        conditions.append(f"{last}.arrival <= ?")
        values.append(arrive_before_utc)

    return conditions, values


def search_flights(
    origin: str,
    destination: str,
    exclude_flight_no: str = "",
    direct_only: bool = False,
    avoid_via: list[str] | None = None,
    depart_after: str = "",
    depart_before: str = "",
    arrive_before: str = "",
) -> dict:
    """Search bookable flights: direct ones, and connections built from two single flights.

    One SQL query does everything: the direct flights, UNION ALL the connections,
    sorted by arrival. The model only chooses the filter values, which always go in
    ? placeholders. Times come in as local times and are converted to UTC here.
    """
    origin = origin.strip().upper()
    destination = destination.strip().upper()
    if not origin or not destination:
        return {"error": "origin and destination are required."}

    exclude_flight_no = exclude_flight_no.strip().upper()
    avoid = [airport.strip().upper() for airport in avoid_via or []]

    # Local times -> UTC, written like the database ("2026-10-23 05:30:00"),
    # so comparing the texts compares the times. A bad format comes back as an error the model can fix.
    try:
        after_utc = local_to_utc(depart_after, origin) if depart_after else ""
        before_utc = local_to_utc(depart_before, origin) if depart_before else ""
        arrive_before_utc = local_to_utc(arrive_before, destination) if arrive_before else ""
    except ValueError as e:
        return {"error": str(e)}

    # 1. Direct flights, with the filters added to the end of their WHERE
    conditions, values = trip_filters("a", "a", exclude_flight_no, avoid, after_utc, before_utc, arrive_before_utc)
    sql = DIRECT_SQL + "".join(f" AND {condition}" for condition in conditions)
    values = [origin, destination] + values

    # 2. Connections, glued under the direct flights with UNION ALL (one list, same columns)
    if not direct_only:
        conditions, filter_values = trip_filters("a", "b", exclude_flight_no, avoid, after_utc, before_utc, arrive_before_utc)
        sql += " UNION ALL " + CONNECTION_SQL + "".join(f" AND {condition}" for condition in conditions)
        # The values in the same order as the ? in CONNECTION_SQL: the two waits, the two airports, then the filters
        values += [f"+{MIN_CONNECTION_MINUTES} minutes", f"+{MAX_CONNECTION_MINUTES} minutes", origin, destination] + filter_values

    # 3. Sort the whole list by arrival and keep the first MAX_ROWS
    sql += " ORDER BY arrival LIMIT ?"
    values.append(MAX_ROWS)

    # Open the database read-only, so nothing here can change the data
    conn = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)
    # With row_factory set to sqlite3.Row, each row remembers its column names,
    # so dict(row) gives {"flight_id": "F-033", "origin": "FCO", ...}
    conn.row_factory = sqlite3.Row
    try:
        trips = [dict(row) for row in conn.execute(sql, values).fetchall()]
    except sqlite3.Error as e:
        return {"error": f"Search failed: {e}"}
    finally:
        conn.close()

    # Add local times, so the model can reason about "tonight" or "before 9" without converting
    for trip in trips:
        trip["departure_local"] = to_local_time(datetime.strptime(trip["departure"], DATABASE_FORMAT), trip["origin"])
        trip["arrival_local"] = to_local_time(datetime.strptime(trip["arrival"], DATABASE_FORMAT), trip["destination"])
        if trip["via"]:
            trip["via_arrival_local"] = to_local_time(datetime.strptime(trip["via_arrival"], DATABASE_FORMAT), trip["via"])
            trip["via_departure_local"] = to_local_time(datetime.strptime(trip["via_departure"], DATABASE_FORMAT), trip["via"])

    return {"flights": trips, "count": len(trips)}


SEARCH_FLIGHTS_TOOL = {
    "type": "function",
    "function": {
        "name": "search_flights",
        "description": (
            "Searches the airline's bookable flights (not yet departed, with free seats) on a route, "
            "with optional filters. Returns matching flights ordered by arrival, with UTC and local "
            "times, or an error message."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "origin": {
                    "type": "string",
                    "description": "Origin airport IATA code, e.g. FCO.",
                },
                "destination": {
                    "type": "string",
                    "description": "Destination airport IATA code, e.g. CDG.",
                },
                "exclude_flight_no": {
                    "type": "string",
                    "description": "A flight number to leave out, e.g. the disrupted flight AU610.",
                },
                "direct_only": {
                    "type": "boolean",
                    "description": "True to return only direct flights.",
                },
                "avoid_via": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Connection airports to avoid, as IATA codes, e.g. [\"FRA\"].",
                },
                "depart_after": {
                    "type": "string",
                    "description": "Earliest departure, local time at the origin, 'YYYY-MM-DD HH:MM'.",
                },
                "depart_before": {
                    "type": "string",
                    "description": "Latest departure, local time at the origin, 'YYYY-MM-DD HH:MM'.",
                },
                "arrive_before": {
                    "type": "string",
                    "description": "Latest arrival, local time at the destination, 'YYYY-MM-DD HH:MM'.",
                },
            },
            "required": ["origin", "destination"],
        },
    },
}
