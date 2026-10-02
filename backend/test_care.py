from datetime import datetime, timedelta, timezone

from backend.models.case_state import (
    CaseState,
    Disruption,
    FlightOption,
    OriginalBooking,
    Passenger,
)
from backend.tools.compute_care_entitlements import compute_care_entitlements

now = datetime.now(timezone.utc)


def make_case(origin, destination, distance_km, disruption_type, delay_minutes=None, new_departure_in=None):
    """Build a case file by hand. new_departure_in: time until the confirmed new flight leaves."""
    original_departure = now + timedelta(hours=1)
    case = CaseState(case_id="test")
    case.passenger = Passenger(id="P-T", name="Test Passenger", contact="test@example.com")
    case.original_booking = OriginalBooking(
        booking_ref="TEST",
        flight_no="AU000",
        origin=origin,
        destination=destination,
        scheduled_departure=original_departure,
        scheduled_arrival=original_departure + timedelta(hours=2),
        distance_km=distance_km,
    )
    case.disruption = Disruption(
        flight_no="AU000",
        type=disruption_type,
        announced_at=now,
        stated_cause="test",
        expected_delay_minutes=delay_minutes,
    )
    if new_departure_in is not None:
        departure = now + new_departure_in
        case.rebooking.confirmed = FlightOption(
            flight_id="F-TEST",
            flight_no="AU999",
            origin=origin,
            destination=destination,
            departure=departure,
            arrival=departure + timedelta(hours=2),
        )
    return case


print("1. Cancellation, not rebooked yet -> error (rebook first)")
print(compute_care_entitlements(make_case("FCO", "CDG", 1105, "cancellation")))

print("\n2. Cancellation, rebooked two days later -> meals, hotel nights, transport")
print(compute_care_entitlements(make_case("FCO", "CDG", 1105, "cancellation", new_departure_in=timedelta(days=2))))

print("\n3. Storm delay of 300 min, 1,455 km (threshold 2h) -> meals, no hotel")
print(compute_care_entitlements(make_case("CDG", "LIS", 1455, "delay", delay_minutes=300)))

print("\n4. Delay of 90 min, 1,105 km (below the 2h threshold) -> nothing owed")
print(compute_care_entitlements(make_case("FCO", "CDG", 1105, "delay", delay_minutes=90)))

print("\n5. Long-haul delay of 200 min, 6,880 km (threshold 4h) -> nothing owed yet")
print(compute_care_entitlements(make_case("FCO", "JFK", 6880, "delay", delay_minutes=200)))