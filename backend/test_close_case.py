from datetime import datetime, timedelta, timezone

from backend.db.inventory import build_inventory_db
from backend.models.case_state import (
    CaseState,
    Compensation,
    Disruption,
    FlightOption,
    OriginalBooking,
    Passenger,
)
from backend.tools.close_case import close_case
from backend.tools.record_rebooking import record_rebooking

now = datetime.now(timezone.utc)


def make_case(booking_ref, disruption_type, delay_minutes=None):
    departure = now + timedelta(hours=1)
    case = CaseState(case_id="test")
    case.passenger = Passenger(id="P-T", name="Test Passenger", contact="test@example.com")
    case.original_booking = OriginalBooking(
        booking_ref=booking_ref, flight_no="AU000", origin="FCO", destination="CDG",
        scheduled_departure=departure, scheduled_arrival=departure + timedelta(hours=2), distance_km=1105,
    )
    case.disruption = Disruption(
        flight_no="AU000", type=disruption_type, announced_at=now,
        stated_cause="test", expected_delay_minutes=delay_minutes,
    )
    return case


def add_compensation(case, amount):
    case.compensation = Compensation(
        eligible=amount > 0, amount_eur=amount, is_extraordinary=amount == 0,
        reasoning="Test reasoning.", sources=[],
    )


def check(description, condition):
    print(f"{'PASS' if condition else 'FAIL'}  {description}")


build_inventory_db()  # fresh database: no rebookings yet

print("\n1. Cancellation, not rebooked -> refuses, asks to rebook")
case = make_case("CLOSE1", "cancellation")
add_compensation(case, 250)
result = close_case(case)
print(f"      {result}")
check("returns an error about rebooking", "error" in result and "Rebook" in result["error"])

print("\n2. Cancellation, rebooked, but compensation not assessed -> refuses")
case = make_case("CLOSE2", "cancellation")
record_rebooking("CLOSE2", "F-002")   # writes the rebooking to the database
result = close_case(case)
print(f"      {result}")
check("returns an error about compensation", "error" in result and "compensation_agent" in result["error"])

print("\n3. Cancellation, rebooked and compensation assessed -> closes, flight from the database")
add_compensation(case, 250)
result = close_case(case)
print(f"      {result}")
check("status is closed", result.get("status") == "closed")
check("flight comes from the database (AU614)", result.get("flight", {}).get("flight_no") == "AU614")
check("compensation is 250 EUR", result.get("compensation", {}).get("amount_eur") == 250)

print("\n4. Delay, passenger keeps the flight, compensation assessed -> closes on the original flight")
case = make_case("CLOSE3", "delay", delay_minutes=240)
add_compensation(case, 250)
result = close_case(case)
print(f"      {result}")
check("status is closed", result.get("status") == "closed")
check("keeps the original flight", result.get("keeps_original_flight") is True)