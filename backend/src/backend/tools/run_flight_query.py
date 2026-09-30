import re
import sqlite3

from backend.db.inventory import DB_PATH

MAX_ROWS = 20  # never return more than this, whatever the query asks for


def run_flight_query(sql: str) -> dict:
    """Run a model-written SQL query against the flight inventory, safely.

    Guardrails, from strongest to lightest:
    1. The connection is read-only: nothing can be changed, whatever the SQL says.
    2. Only a single SELECT is accepted.
    3. Only the available_flights view may be queried, not the raw table.
    4. At most MAX_ROWS rows are returned.
    Problems come back as {"error": ...} so the model can fix its query.
    """
    query = sql.strip().rstrip(";").strip()

    if not query.lower().startswith("select"):
        return {"error": "Only SELECT queries are allowed."}

    if ";" in query:
        return {"error": "Only a single query is allowed. Remove the extra statements."}

    # \bflights\b matches the word "flights" on its own, but not inside "available_flights"
    if re.search(r"\bflights\b", query, re.IGNORECASE):
        return {"error": "Query the available_flights view, not the flights table."}

    # mode=ro opens the file read-only: the database itself refuses any change
    conn = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)
    # Without this line, the row comes back as a tuple, just the values in order with no names:("AZ608", "FCO", "JFK")
    # With this line, it comes back as a sqlite3.Row, which keeps the column names, so it can be converted later into:
    #  {"flight_no": "AZ608", "origin": "FCO", "destination": "JFK"}
    conn.row_factory = sqlite3.Row  # rows behave like dictionaries, with column names

    try:
        # execute searches until it finds the first matching row, then pauses and waits.
        cursor = conn.execute(query)
        rows = cursor.fetchmany(MAX_ROWS)
        
        return {
           # This is what the rows look like, as a list of sqlite3.Row objects:
           #     [
           #         {"flight_no": "AZ608",  "destination": "JFK"},
           #         {"flight_no": "AF1305", "destination": "CDG"},
            #    ]
            "rows": [dict(row) for row in rows],
            "row_count": len(rows),
        }
    except sqlite3.Error as e:
        
        return {"error": f"SQL error: {e}"}
    finally:
        conn.close()


RUN_FLIGHT_QUERY_TOOL = {
    "type": "function",
    "function": {
        "name": "run_flight_query",
        "description": (
            "Runs one read-only SQLite SELECT query on the available_flights table "
            "and returns the matching rows (at most 20), or an error message."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "sql": {
                    "type": "string",
                    "description": "The SELECT query to run.",
                },
            },
            "required": ["sql"],
        },
    },
}