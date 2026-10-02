from datetime import datetime, timedelta, timezone

from backend.models.case_state import (
    CaseState,
    Disruption,
    FlightOption,
    OriginalBooking,
    Passenger,
)
from backend.tools.calculate_compensation import calculate_compensation

now = datetime.now(timezone.utc)


def make_case(distance_km, disruption_type, delay_minutes=None, rebooked_arrival_late_by=None):
    """Build a case by hand. rebooked_arrival_late_by: how much later than planned the new flight arrives."""
    departure = now + timedelta(hours=1)
    arrival = departure + timedelta(hours=2)
    case = CaseState(case_id="test")
    case.passenger = Passenger(id="P-T", name="Test Passenger", contact="test@example.com")
    case.original_booking = OriginalBooking(
        booking_ref="TEST", flight_no="AU000", origin="FCO", destination="CDG",
        scheduled_departure=departure, scheduled_arrival=arrival, distance_km=distance_km,
    )
    case.disruption = Disruption(
        flight_no="AU000", type=disruption_type, announced_at=now - timedelta(hours=1),
        stated_cause="test", expected_delay_minutes=delay_minutes,
    )
    if rebooked_arrival_late_by is not None:
        case.rebooking.confirmed = FlightOption(
            flight_id="F-TEST", flight_no="AU999", origin="FCO", destination="CDG",
            departure=departure + rebooked_arrival_late_by,
            arrival=arrival + rebooked_arrival_late_by,
        )
    return case


def show(title, result, expected_amount):
    status = "PASS" if result.get("amount_eur") == expected_amount else "FAIL"
    print(f"{status}  {title}: {result.get('amount_eur')} EUR (expected {expected_amount})")
    print(f"      {result.get('rule_applied') or result.get('error')}")


show("Technical fault, 1,105 km, rebooked 2 days later",
     calculate_compensation(make_case(1105, "cancellation", rebooked_arrival_late_by=timedelta(days=2)), False), 250)

show("Short notice, new flight arrives only 90 min late (exempt under Art. 5(1)(c)(iii))",
     calculate_compensation(make_case(1105, "cancellation", rebooked_arrival_late_by=timedelta(minutes=90)), False), 0)

show("Long-haul, new flight arrives 3 hours late (50% reduction)",
     calculate_compensation(make_case(6880, "cancellation", rebooked_arrival_late_by=timedelta(hours=3)), False), 300)

show("Crew strike (not extraordinary), 6,880 km, rebooked next day",
     calculate_compensation(make_case(6880, "cancellation", rebooked_arrival_late_by=timedelta(hours=22)), False), 600)

show("Storm (extraordinary), 1,455 km, delay of 300 min",
     calculate_compensation(make_case(1455, "delay", delay_minutes=300), True), 0)

show("Same delay of 300 min, but NOT extraordinary",
     calculate_compensation(make_case(1455, "delay", delay_minutes=300), False), 250)

show("Delay of 150 min (under 3 hours)",
     calculate_compensation(make_case(1105, "delay", delay_minutes=150), False), 0)

result = calculate_compensation(make_case(1105, "cancellation"), False)
print(f"{'PASS' if 'error' in result else 'FAIL'}  Cancellation not rebooked yet -> error")
print(f"      {result.get('error')}")