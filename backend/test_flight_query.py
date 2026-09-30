from backend.db.inventory import build_inventory_db
from backend.tools.run_flight_query import run_flight_query

build_inventory_db()  # make sure the database exists with fresh times

print("1. Valid query, Rome to New York:")
print(run_flight_query(
    "SELECT flight_no, via, departure, seats_available "
    "FROM available_flights WHERE origin = 'FCO' AND destination = 'JFK'"
))

print("\n2. Not a SELECT (should be rejected):")
print(run_flight_query("DELETE FROM available_flights"))

print("\n3. Raw table instead of the view (should be rejected):")
print(run_flight_query("SELECT * FROM flights"))

print("\n4. SQL typo (should return an SQL error the model could fix):")
print(run_flight_query("SELECT flight_no FROM available_flights WHERE orign = 'FCO'"))