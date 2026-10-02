import sqlite3

from backend.db.inventory import DB_PATH, build_inventory_db
from backend.tools.record_rebooking import record_rebooking


def seats(flight_id: str) -> int:
    """How many seats a flight has right now, read straight from the database."""
    conn = sqlite3.connect(DB_PATH)
    row = conn.execute(
        "SELECT seats_available FROM flights WHERE flight_id = ?", [flight_id]
    ).fetchone()
    conn.close()
    return row[0]


def rebooked_flight(booking_ref: str) -> str | None:
    """Which flight a booking is rebooked on, or None if it isn't."""
    conn = sqlite3.connect(DB_PATH)
    row = conn.execute(
        "SELECT flight_id FROM rebookings WHERE booking_ref = ?", [booking_ref]
    ).fetchone()
    conn.close()
    return row[0] if row else None


def check(description: str, condition: bool) -> None:
    """Print PASS or FAIL for one expectation, so every check is visible."""
    print(f"{'PASS' if condition else 'FAIL'}  {description}")


# Fresh database: original seat counts, no rebookings
build_inventory_db()

print("\n1. First booking on F-002 (had 4 seats)")
result = record_rebooking("test01", "F-002")
print(result)
check("returns a confirmation", result.get("status") == "confirmed")
check("F-002 now has 3 seats", seats("F-002") == 3)
check("TEST01 is rebooked on F-002", rebooked_flight("TEST01") == "F-002")
check("confirmation has local times", "departure_local" in result and "arrival_local" in result)

print("\n2. Same booking, same flight again (must not take a second seat)")
result = record_rebooking("TEST01", "F-002")
check("returns a confirmation again", result.get("status") == "confirmed")
check("F-002 still has 3 seats", seats("F-002") == 3)

print("\n3. Full flight F-001")
result = record_rebooking("TEST02", "F-001")
print(result)
check("returns an error", "error" in result)
check("F-001 still has 0 seats", seats("F-001") == 0)
check("TEST02 is not rebooked", rebooked_flight("TEST02") is None)

print("\n4. Already departed flight F-005")
result = record_rebooking("TEST03", "F-005")
print(result)
check("returns an error", "error" in result)
check("TEST03 is not rebooked", rebooked_flight("TEST03") is None)

print("\n5. Flight that doesn't exist")
result = record_rebooking("TEST04", "F-999")
check("returns an error", "error" in result)

print("\n6. Empty inputs")
result = record_rebooking("", "F-002")
check("empty booking_ref returns an error", "error" in result)
result = record_rebooking("TEST05", "")
check("empty flight_id returns an error", "error" in result)

print("\n7. TEST01 changes their mind: F-002 -> F-003 (had 38 seats)")
result = record_rebooking("TEST01", "F-003")
print(result)
check("returns a confirmation", result.get("status") == "confirmed")
check("F-002 got its seat back (4 seats)", seats("F-002") == 4)
check("F-003 now has 37 seats", seats("F-003") == 37)
check("TEST01 is now rebooked on F-003", rebooked_flight("TEST01") == "F-003")