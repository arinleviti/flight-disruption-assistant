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
# we use the three quotes when the string is long and we want to keep the formatting, but it could be a single-quoted string too.
DIRECT_SQL = """
    SELECT a.flight_id, a.flight_no, a.origin, a.destination, NULL AS via,
           a.departure, a.arrival, NULL AS via_arrival, NULL AS via_departure,
           a.seats_available
    FROM available_flights a
    WHERE a.origin = ? AND a.destination = ?
"""
#The query builds a temporary table in memory with the columns you named using AS.
#  Python reads it, and then it’s thrown away. The real available_flights table never changes.
# MIN gives the smallest of 2 values, so the connection's available seats is the smaller of the two flights' available seats.
# Connections: the same table twice, as "a" (first flight) and "b" (second flight).
# In SQL || joins two pieces of text, same as + in Typescript
# JOIN ... ON keeps only the pairs where the first flight lands at the airport the second one leaves from,
# with enough time to change planes in between. The database builds the pairs, not Python.
# the SELECT part is written first, but it runs last. SQLite runs the query in this order:
# FROM … JOIN … ON: pair up flights.
# WHERE: throw away the pairs that don’t fit.
# SELECT: build one row for each pair that’s left.
# datetime() takes a date in that format, adds an amount of time to it, and gives you back the new date in the same format.
# datetime('2026-10-23 07:40:00', '+45 minutes')    →   '2026-10-23 08:25:00'
# WHERE starts the list of filter rules. AND adds another rule to that list.
#A query can only have one WHERE. After that, every extra rule gets attached with AND
# in my case, AND is before WHERE because they are part of the ON above.
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

# The two SQL queries above only answer: “what flights go from FCO to CDG?” That gives you every flight on the route.
# the passenger says things like:“Not AU610, that’s the one that got cancelled.”
# “I don’t want to go through Frankfurt.”
# “I can’t leave before 2pm.”
# “I need to be in Paris by 9pm.” trip_filters exists to add those extra rules on top.

# Direct flight -> trip_filters("a", "a", ...)
#   conditions = ["a.flight_no != ?", "a.departure >= ?"]
#   values     = ["AU610",            "2026-10-23 12:00:00"]
#   (no FRA rule: a direct flight has no connection airport)
#
# Connection -> trip_filters("a", "b", ...)
#   conditions = ["a.flight_no != ?", "b.flight_no != ?", "a.destination != ?", "a.departure >= ?"]
#   values     = ["AU610",            "AU610",            "FRA",                "2026-10-23 12:00:00"]
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
    # this creates the SQL string with the extra conditions, e.g. "SELECT ... FROM ... WHERE a.origin = ? AND a.destination = ? AND a.flight_no != ? AND a.departure >= ?"
    # f" AND {condition}" for condition in conditions goes through each condition and puts " AND " in front
    # "".join(...) glues those pieces into one string. The "" is what goes between the pieces, which here is nothing
    sql = DIRECT_SQL + "".join(f" AND {condition}" for condition in conditions)
    # + on two lists joins them into one, like [...a, ...b] in TS: ["FCO", "CDG"] + ["AU610", "2026-10-23 12:00:00"] → ["FCO", "CDG", "AU610", "2026-10-23 12:00:00"]
    values = [origin, destination] + values

    # 2. Connections, glued under the direct flights with UNION ALL (one list, same columns)
    if not direct_only:
        conditions, filter_values = trip_filters("a", "b", exclude_flight_no, avoid, after_utc, before_utc, arrive_before_utc)
        # here we add the previous sql to the connection query.
        # UNION ALL keeps all the rows from both queries, even if they are the same. The connection query has its own WHERE, so we add the passenger's filters to that with AND.
        # check comment 1.
        sql += " UNION ALL " + CONNECTION_SQL + "".join(f" AND {condition}" for condition in conditions)
        # The values in the same order as the ? in CONNECTION_SQL: the two waits, the two airports, then the filters
        # This line adds the connection query’s values to the end of the values list.
        # check comment 2.
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
        # conn.execute(sql, values). It hands SQLite two things: sql: the text, still full of ? values: the list 
        # SQLite then does the matching inside itself
        # dict(row) turns each row into a dict, like {"flight_id": "F-033+F-047", "origin": "FCO", ...}
        # we go through all this pain for a reason: With placeholders, the database reads the SQL first and fixes what the query does;
        # the values are added afterwards and only ever compared, NEVER READ AS INSTRUCTIONS. So nothing the model sends can change the query.
        # AU610' OR '1'='1 will always be treated as a string, not as SQL code. The database never sees it as part of the query.
        # this is safe from SQL injection attacks, which is why we never build the query by concatenating strings with the values in them.
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
    # check comment 3.
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

        # COMMENT 1. After the next line, with the example filters (not AU610, not via FRA, leave after 14:00 Rome),
        # sql looks like this:
        #
        #   SELECT a.flight_id, a.flight_no, a.origin, a.destination, NULL AS via,
        #          a.departure, a.arrival, NULL AS via_arrival, NULL AS via_departure,
        #          a.seats_available
        #   FROM available_flights a
        #   WHERE a.origin = ? AND a.destination = ?
        #    AND a.flight_no != ? AND a.departure >= ?
        #   UNION ALL
        #   SELECT a.flight_id || '+' || b.flight_id AS flight_id,
        #          a.flight_no || '/' || b.flight_no AS flight_no,
        #          a.origin AS origin, b.destination AS destination, a.destination AS via,
        #          a.departure AS departure, b.arrival AS arrival,
        #          a.arrival AS via_arrival, b.departure AS via_departure,
        #          MIN(a.seats_available, b.seats_available) AS seats_available
        #   FROM available_flights a
        #   JOIN available_flights b
        #     ON a.destination = b.origin
        #    AND b.departure >= datetime(a.arrival, ?)
        #    AND b.departure <= datetime(a.arrival, ?)
        #   WHERE a.origin = ? AND b.destination = ?
        #    AND a.flight_no != ? AND b.flight_no != ? AND a.destination != ? AND a.departure >= ?
        #
        # Lines up to UNION ALL: the direct flights (already built above).
        # Lines after UNION ALL: CONNECTION_SQL, plus the filters from this trip_filters call.

        # COMMENT 2.
        # After the next line, with the example filters, values looks like this
        # (one value per ?, in the order the ? appear in sql):
        #
        #   [
        #     "FCO",                  # a.origin = ?               } direct query,
        #     "CDG",                  # a.destination = ?          } already in values
        #     "AU610",                # a.flight_no != ?           }
        #     "2026-10-23 12:00:00",  # a.departure >= ?           }
        #
        #     "+45 minutes",          # datetime(a.arrival, ?)     } added by this line:
        #     "+360 minutes",         # datetime(a.arrival, ?)     } CONNECTION_SQL's own ?
        #     "FCO",                  # a.origin = ?               }
        #     "CDG",                  # b.destination = ?          }
        #
        #     "AU610",                # a.flight_no != ?           } added by this line:
        #     "AU610",                # b.flight_no != ?           } filter_values
        #     "FRA",                  # a.destination != ?         }
        #     "2026-10-23 12:00:00",  # a.departure >= ?           }
        #   ]

    # COMMENT 3: What the model gets back, e.g. for FCO -> CDG (sorted by arrival, so the connection comes first).
    # The *_local values are whatever to_local_time returns; shown here as local clock times.
    #
    # {
    #   "flights": [
    #     {                                        # a connection: two flights, via MUC
    #       "flight_id": "F-033+F-047",
    #       "flight_no": "AU101/AU220",
    #       "origin": "FCO",
    #       "destination": "CDG",
    #       "via": "MUC",
    #       "departure": "2026-10-23 06:00:00",       # UTC, leaves FCO
    #       "arrival": "2026-10-23 10:30:00",         # UTC, lands at CDG
    #       "via_arrival": "2026-10-23 07:40:00",     # UTC, lands at MUC
    #       "via_departure": "2026-10-23 08:50:00",   # UTC, leaves MUC
    #       "seats_available": 3,                     # the smaller of the two flights
    #       "departure_local": "2026-10-23 08:00",    # Rome time
    #       "arrival_local": "2026-10-23 12:30",      # Paris time
    #       "via_arrival_local": "2026-10-23 09:40",  # Munich time
    #       "via_departure_local": "2026-10-23 10:50" # Munich time
    #     },
    #     {                                        # a direct flight: via fields are None, no via_*_local keys
    #       "flight_id": "F-061",
    #       "flight_no": "AU612",
    #       "origin": "FCO",
    #       "destination": "CDG",
    #       "via": None,
    #       "departure": "2026-10-23 14:20:00",
    #       "arrival": "2026-10-23 16:10:00",
    #       "via_arrival": None,
    #       "via_departure": None,
    #       "seats_available": 12,
    #       "departure_local": "2026-10-23 16:20",
    #       "arrival_local": "2026-10-23 18:10"
    #     }
    #   ],
    #   "count": 2                                 # how many trips are in "flights" (at most MAX_ROWS)
    # }
    #
    # If something goes wrong earlier, the function returns {"error": "..."} instead.