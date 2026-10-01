import sqlite3
from datetime import datetime

from backend.db.inventory import DB_PATH
from backend.tools.time_utils import DATABASE_FORMAT, local_to_utc, to_local_time

MAX_ROWS = 20  # never return more than this


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
    """Search bookable flights with fixed, pre-written SQL.

    The model only chooses the filter values. Each filter adds one condition with a
    ? placeholder, so its values never become part of the SQL text itself.
    Times come in as local times and are converted to UTC here, in code.
    """
    origin = origin.strip().upper()
    destination = destination.strip().upper()
    if not origin or not destination:
        return {"error": "origin and destination are required."}

    conditions = ["origin = ?", "destination = ?"]
    values = [origin, destination]

    if exclude_flight_no:
        conditions.append("flight_no != ?")
        values.append(exclude_flight_no.strip().upper())

    if direct_only:
        conditions.append("via IS NULL")

    for airport in avoid_via or []:
        conditions.append("(via IS NULL OR via != ?)")
        values.append(airport.strip().upper())

    # Local times -> UTC. A bad format comes back as an error the model can fix.
    try:
        if depart_after:
            conditions.append("departure >= ?")
            values.append(local_to_utc(depart_after, origin))
        if depart_before:
            conditions.append("departure <= ?")
            values.append(local_to_utc(depart_before, origin))
        if arrive_before:
            conditions.append("arrival <= ?")
            values.append(local_to_utc(arrive_before, destination))
    except ValueError as e:
        return {"error": str(e)}

    # Only the column names and the structure are written here; every value is a placeholder
    sql = (
        "SELECT flight_id, flight_no, origin, destination, via, departure, arrival, seats_available "
        "FROM available_flights "
        "WHERE " + " AND ".join(conditions) + " "
        "ORDER BY arrival "
        "LIMIT ?"
    )
    values.append(MAX_ROWS)

    # Open the database file, like opening a spreadsheet. conn (for "connection") is how you talk to it from now on.
    # The "file:...mode=ro" URI means "read-only", so the model can't accidentally change the data. uri=True is needed to tell sqlite3 that this is a URI, not a file path.
    conn = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)
    # With this line, each row also remembers its column names, so it can become {"flight_id": "F-010", "via": "LHR", ...}.
    conn.row_factory = sqlite3.Row

    try:
        #cursor = conn.execute(sql, values)    # 1. run the query, filling the ? blanks with values
        #found = cursor.fetchall()             # 2. collect every matching row
        #rows = []
        #for row in found:                     # 3. turn each row into a normal dictionary
        #    rows.append(dict(row))            dict is necessary because sqlite3.Row is not a normal dictionary, it's a SQLite own type.

        # ends up becoming like this:
        #[
        #   {"flight_id": "F-010", "flight_no": "AU410/AU161", "origin": "FCO",
        #   "destination": "JFK", "via": "LHR", "departure": "2026-10-01 18:36:49",
        #   "arrival": "2026-10-02 07:36:49", "seats_available": 6},
        #   {"flight_id": "F-011", "flight_no": "AU410/AU161", "origin": "FCO",
        #   "destination": "JFK", "via": "LHR", "departure": "2026-10-01 19:36:49", "arrival": "2026-10-02 08:36:49", "seats_available": 6},
        #   ...
        #]
        rows = [dict(row) for row in conn.execute(sql, values).fetchall()]
    except sqlite3.Error as e:
        return {"error": f"Search failed: {e}"}
    finally:
        conn.close()

    # Add local times, so the model can reason about "tonight" or "before 9" without converting
    for row in rows:
        departure = datetime.strptime(row["departure"], DATABASE_FORMAT)
        arrival = datetime.strptime(row["arrival"], DATABASE_FORMAT)
        row["departure_local"] = to_local_time(departure, row["origin"])
        row["arrival_local"] = to_local_time(arrival, row["destination"])

    return {"flights": rows, "count": len(rows)}


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