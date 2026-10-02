import json
import sqlite3
from datetime import timedelta
from pathlib import Path

from backend.tools.get_booking import convert_offset_to_time
from backend.tools.time_utils import local_day_time_to_utc

# backend/src/backend/db/inventory.py -> parents[3] is the backend root folder
DATA_DIR = Path(__file__).resolve().parents[3] / "data"
FLIGHTS_PATH = DATA_DIR / "flights.json"
DB_PATH = DATA_DIR / "inventory.db"

# SQLite's own date format, so that comparisons with datetime('now') work
SQLITE_DATE_FORMAT = "%Y-%m-%d %H:%M:%S"


def build_inventory_db() -> None:
    """Rebuild the flight inventory database from flights.json.

    Runs at every server start. A flight's departure is written in one of two ways:
    - "departure_offset_minutes": minutes from now (for scenarios that must always
      happen soon, like a full flight or one that just left)
    - "departure_day" + "departure_time": a day (0 = today, 1 = tomorrow...) and a
      local time at the origin airport, so "tomorrow at 07:15" is always morning.
    Rebookings start empty on every build.
    """
    conn = sqlite3.connect(DB_PATH)

    # Start fresh on every build (the view first, since it depends on the flights table)
    conn.execute("DROP VIEW IF EXISTS available_flights")
    conn.execute("DROP TABLE IF EXISTS rebookings")
    conn.execute("DROP TABLE IF EXISTS flights")

    conn.execute("""
        CREATE TABLE flights (
            flight_id       TEXT PRIMARY KEY,
            flight_no       TEXT NOT NULL,
            origin          TEXT NOT NULL,
            destination     TEXT NOT NULL,
            via             TEXT,
            departure       TEXT NOT NULL,
            arrival         TEXT NOT NULL,
            seats_available INTEGER NOT NULL
        )
    """)

    # One row per confirmed rebooking. booking_ref is the primary key,
    # so a booking can only ever have one rebooking.
    # In the same file as flights, so taking a seat and saving the rebooking
    # can happen in one transaction: both succeed or neither does.
    conn.execute("""
        CREATE TABLE rebookings (
            booking_ref TEXT PRIMARY KEY,
            flight_id   TEXT NOT NULL,
            flight_no   TEXT NOT NULL,
            booked_at   TEXT NOT NULL
        )
    """)

    flights = json.loads(FLIGHTS_PATH.read_text(encoding="utf-8"))

    rows = []
    for flight in flights:
        if "departure_offset_minutes" in flight:
            departure = convert_offset_to_time(flight["departure_offset_minutes"])
        else:
            departure = local_day_time_to_utc(
                flight["departure_day"], flight["departure_time"], flight["origin"]
            )
        arrival = departure + timedelta(minutes=flight["duration_minutes"])

        rows.append((
            flight["flight_id"],
            flight["flight_no"],
            flight["origin"],
            flight["destination"],
            flight["via"],                                   # None for direct flights
            departure.strftime(SQLITE_DATE_FORMAT),
            arrival.strftime(SQLITE_DATE_FORMAT),
            flight["seats_available"],
        ))

    conn.executemany(
        "INSERT INTO flights VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        rows,
    )

    # Flights that haven't left yet and still have seats
    conn.execute("""
        CREATE VIEW available_flights AS
        SELECT flight_id, flight_no, origin, destination, via, departure, arrival, seats_available
        FROM flights
        WHERE departure > datetime('now')
          AND seats_available > 0
    """)

    conn.commit()
    conn.close()