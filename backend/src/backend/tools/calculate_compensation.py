from datetime import datetime, timedelta, timezone

from backend.models.case_state import CaseState


def as_utc(moment: datetime) -> datetime:
    """Times from the database have no time zone attached: label them as UTC."""
    if moment.tzinfo is None:
        return moment.replace(tzinfo=timezone.utc)
    return moment


def minutes_between(start: datetime, end: datetime) -> int:
    """Whole minutes from start to end (negative if end is before start)."""
    return int((end - start).total_seconds() // 60)


def distance_band(distance_km: int) -> tuple[int, int]:
    """EU261 Art. 7: the full amount, and the arrival window for the 50% reduction.

    Simplification: the regulation puts intra-EU flights over 1,500 km in the
    middle band whatever their length; here we go by distance only.
    """
    if distance_km <= 1500:
        return 250, 120
    if distance_km <= 3500:
        return 400, 180
    return 600, 240


def calculate_compensation(case: CaseState, is_extraordinary: bool) -> dict:
    """Work out the EU261 compensation from the case file. No LLM, only rules.

    The only judgment comes from outside: is_extraordinary, decided by the
    compensation agent after reading the legal texts.
    """
    booking = case.original_booking
    disruption = case.disruption
    if booking is None or disruption is None:
        return {"error": "The booking and the disruption must be identified first."}

    full_amount, reduction_window = distance_band(booking.distance_km)
    scheduled_departure = as_utc(booking.scheduled_departure)
    scheduled_arrival = as_utc(booking.scheduled_arrival)

    # When does the passenger actually leave and arrive?
    confirmed = case.rebooking.confirmed
    if confirmed is not None:
        new_departure = as_utc(confirmed.departure)
        new_arrival = as_utc(confirmed.arrival)
    elif disruption.type == "delay" and disruption.expected_delay_minutes:
        delay = timedelta(minutes=disruption.expected_delay_minutes)
        new_departure = scheduled_departure + delay
        new_arrival = scheduled_arrival + delay
    else:
        return {
            "error": (
                "The new arrival time isn't known yet. Rebook the passenger first: "
                "compensation depends on how late they arrive."
            )
        }

    arrival_delay = minutes_between(scheduled_arrival, new_arrival)
    notice_days = (scheduled_departure - as_utc(disruption.announced_at)).total_seconds() / 86400

    amount = 0
    if is_extraordinary:
        rule = "No compensation: extraordinary circumstances (Art. 5(3))."

    elif disruption.type == "delay":
        if arrival_delay >= 180:
            amount = full_amount
            rule = "Arrival delay of 3 hours or more: compensation as for a cancellation (Sturgeon ruling)."
        else:
            rule = "No compensation: the arrival delay is under 3 hours."

    else:  # cancellation
        # Positive if the new flight leaves EARLIER than the cancelled one was due to
        left_earlier = minutes_between(new_departure, scheduled_departure)

        if notice_days >= 14:
            rule = "No compensation: informed at least two weeks before departure (Art. 5(1)(c)(i))."
        elif notice_days >= 7 and left_earlier <= 120 and arrival_delay < 240:
            rule = (
                "No compensation: informed 7 to 14 days before, with a new flight leaving at most "
                "2 hours earlier and arriving less than 4 hours later (Art. 5(1)(c)(ii))."
            )
        elif notice_days < 7 and left_earlier <= 60 and arrival_delay < 120:
            rule = (
                "No compensation: informed less than 7 days before, with a new flight leaving at most "
                "1 hour earlier and arriving less than 2 hours later (Art. 5(1)(c)(iii))."
            )
        elif arrival_delay <= reduction_window:
            amount = full_amount // 2
            rule = (
                f"Compensation reduced by 50%: the new flight arrives within "
                f"{reduction_window // 60} hours of the original arrival (Art. 7(2))."
            )
        else:
            amount = full_amount
            rule = "Full compensation for the cancellation (Art. 5(1)(c) and Art. 7(1))."

    return {
        "eligible": amount > 0,
        "amount_eur": amount,
        "is_extraordinary": is_extraordinary,
        "rule_applied": rule,
        "details": {
            "distance_km": booking.distance_km,
            "disruption_type": disruption.type,
            "arrival_delay_minutes": arrival_delay,
            "notice_days": round(notice_days, 1),
        },
    }