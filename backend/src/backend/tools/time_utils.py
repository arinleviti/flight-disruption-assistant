from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

# Every airport in our data, with its time zone.
# Add a line here whenever a new airport appears in the JSON files.
AIRPORT_TIMEZONES = {
    "FCO": "Europe/Rome",       # Rome Fiumicino
    "MXP": "Europe/Rome",       # Milan Malpensa
    "CDG": "Europe/Paris",      # Paris Charles de Gaulle
    "LIS": "Europe/Lisbon",     # Lisbon
    "MAD": "Europe/Madrid",     # Madrid
    "FRA": "Europe/Berlin",     # Frankfurt
    "LHR": "Europe/London",     # London Heathrow
    "JFK": "America/New_York",  # New York JFK
}

LOCAL_FORMAT = "%Y-%m-%d %H:%M"         # how local times are written, e.g. 2026-10-01 22:41
DATABASE_FORMAT = "%Y-%m-%d %H:%M:%S"   # how UTC times are stored in SQLite


def to_local_time(utc_time: datetime, airport: str) -> str:
    """Turn a UTC time into the local time at an airport, as text.

    Times from the database have no time zone attached, so we first state
    that they are UTC, then let ZoneInfo apply the airport's rules
    (including summer/winter time for that date).
    """
    if utc_time.tzinfo is None:
        utc_time = utc_time.replace(tzinfo=timezone.utc)

    zone_name = AIRPORT_TIMEZONES.get(airport)
    if zone_name is None:
        # Unknown airport: better to show UTC, clearly labelled, than a wrong local time
        return utc_time.strftime(LOCAL_FORMAT) + " UTC"

    return utc_time.astimezone(ZoneInfo(zone_name)).strftime(LOCAL_FORMAT)


def local_to_utc(local_text: str, airport: str) -> str:
    """Turn a local time at an airport ('YYYY-MM-DD HH:MM') into UTC, in the database format.

    The opposite of to_local_time. Raises ValueError with a clear message if the
    text or the airport is wrong, so the caller can pass the message to the model.
    """
    zone_name = AIRPORT_TIMEZONES.get(airport)
    if zone_name is None:
        raise ValueError(f"Unknown airport '{airport}'.")

    try:
        local_time = datetime.strptime(local_text.strip(), LOCAL_FORMAT)
    except ValueError:
        raise ValueError(
            f"Time '{local_text}' must be written as 'YYYY-MM-DD HH:MM' (local time at {airport})."
        )

    # Label the time with the airport's zone, then convert it to UTC
    local_time = local_time.replace(tzinfo=ZoneInfo(zone_name))
    return local_time.astimezone(timezone.utc).strftime(DATABASE_FORMAT)


def local_day_time_to_utc(day: int, time_text: str, airport: str) -> datetime:
    """A flight time written as 'day + local time' (e.g. tomorrow at 07:15 in Rome) -> UTC datetime.

    day is 0 for today, 1 for tomorrow, and so on, counted in the airport's own
    time zone, so "tomorrow" means tomorrow in Rome even if UTC is a different date.
    """
    zone = ZoneInfo(AIRPORT_TIMEZONES[airport])
    local_date = datetime.now(zone).date() + timedelta(days=day)
    hour, minute = (int(part) for part in time_text.split(":"))

    local_time = datetime(local_date.year, local_date.month, local_date.day, hour, minute, tzinfo=zone)
    return local_time.astimezone(timezone.utc)


def current_local_time(airport: str) -> str:
    """The current local date and time at an airport, e.g. '2026-10-01 15:30'."""
    return to_local_time(datetime.now(timezone.utc), airport)