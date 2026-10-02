import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from backend.models.case_state import CaseState
from backend.tools.time_utils import AIRPORT_TIMEZONES, to_local_time

# backend/src/backend/tools/compute_care_entitlements.py -> parents[3] is the backend root folder
POLICY_PATH = Path(__file__).resolve().parents[3] / "data" / "care_policies.json"


def meal_threshold_minutes(distance_km: int) -> int:
    """EU261 Art. 6(1): how long a delay must last before meals are owed.

    Simplification: the regulation puts intra-EU flights over 1,500 km in the
    3-hour band whatever their length; here we go by distance only.
    """
    if distance_km <= 1500:
        return 120
    if distance_km <= 3500:
        return 180
    return 240


def as_utc(moment: datetime) -> datetime:
    """Times from the database have no time zone attached: label them as UTC."""
    if moment.tzinfo is None:
        return moment.replace(tzinfo=timezone.utc)
    return moment


def compute_care_entitlements(case: CaseState) -> dict:
    """Work out the care a passenger is owed, from the case file. No LLM, only rules.

    - meals: always for a cancellation; for a delay, once the wait reaches the threshold
    - hotel + transport: when the new departure is at least the day after the original one
    - two communications whenever care applies
    The cause of the disruption does not matter: care is owed even in extraordinary circumstances.
    """
    booking = case.original_booking
    disruption = case.disruption
    if booking is None or disruption is None:
        return {"error": "The booking and the disruption must be identified first."}

    original_departure = as_utc(booking.scheduled_departure)

    # When will the passenger actually leave?
    if case.rebooking.confirmed is not None:
        new_departure = as_utc(case.rebooking.confirmed.departure)
        based_on = f"confirmed new flight {case.rebooking.confirmed.flight_no}"
    elif disruption.type == "delay" and disruption.expected_delay_minutes:
        new_departure = original_departure + timedelta(minutes=disruption.expected_delay_minutes)
        based_on = f"expected delay of {disruption.expected_delay_minutes} minutes"
    else:
        return {
            "error": (
                "There is no new departure time yet. Rebook the passenger first: "
                "care depends on when they will leave."
            )
        }

    wait_minutes = max(0, int((new_departure - original_departure).total_seconds() // 60))

    # Meals: always for a cancellation, otherwise only above the threshold
    threshold = meal_threshold_minutes(booking.distance_km)
    meals = disruption.type == "cancellation" or wait_minutes >= threshold

    # Hotel: count the nights in the departure airport's own calendar
    zone = ZoneInfo(AIRPORT_TIMEZONES[booking.origin])
    original_date = original_departure.astimezone(zone).date()
    new_date = new_departure.astimezone(zone).date()
    hotel_nights = max(0, (new_date - original_date).days)
    transport = hotel_nights > 0

    communications = 2 if (meals or hotel_nights > 0) else 0

    # The airline's policy at this airport: what the vouchers are worth
    policies = json.loads(POLICY_PATH.read_text(encoding="utf-8"))
    policy = policies.get(booking.origin)
    if policy is None:
        return {"error": f"No care policy found for airport {booking.origin}."}

    meal_vouchers = 0
    if meals:
        minutes_per_voucher = policy["hours_per_meal_voucher"] * 60
        meal_vouchers = min(policy["max_meal_vouchers"], 1 + wait_minutes // minutes_per_voucher)

    return {
        "entitlements": {
            "meals": meals,
            "hotel_nights": hotel_nights,
            "transport": transport,
            "communications": communications,
        },
        "vouchers": {
            "meal": {"count": meal_vouchers, "amount_eur_each": policy["meal_voucher_eur"]},
            "transport": {"count": 1 if transport else 0, "amount_eur_each": policy["transport_voucher_eur"]},
        },
        "basis": {
            "original_departure_local": to_local_time(original_departure, booking.origin),
            "new_departure_local": to_local_time(new_departure, booking.origin),
            "wait_minutes": wait_minutes,
            "meal_threshold_minutes": threshold,
            "based_on": based_on,
        },
        "note": "Care is owed whatever the cause of the disruption, including extraordinary circumstances.",
    }


COMPUTE_CARE_ENTITLEMENTS_TOOL = {
    "type": "function",
    "function": {
        "name": "compute_care_entitlements",
        "description": (
            "Works out the care the passenger is owed (meals, hotel nights, transport, "
            "communications) and the value of the vouchers at the departure airport. Reads "
            "the booking, the disruption and the confirmed new flight from the case file, so "
            "it takes no arguments. Returns an error if the passenger hasn't been rebooked yet."
        ),
        "parameters": {
            "type": "object",
            "properties": {},
            "required": [],
        },
    },
}