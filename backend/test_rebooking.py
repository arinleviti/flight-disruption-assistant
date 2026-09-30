from backend.db.inventory import build_inventory_db
from backend.agents.rebooking_agent import get_flights_options

build_inventory_db()
print(get_flights_options("FCO", "JFK", "AU901", special_needs="wheelchair assistance"))