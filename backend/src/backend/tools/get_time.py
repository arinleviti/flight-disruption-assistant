from .time_utils import current_local_time, AIRPORT_TIMEZONES


def get_time(airport_code) -> dict:  # -> dict[str, str] | dict[str, Any]:
    """
    Returns the current time at the given airport code.

    Args:
        airport_code (str): The IATA code of the airport.

    Returns:
        str: The current time at the airport in HH:MM format.
    """
    airport = airport_code.strip().upper()
    # An airport we don't know: say so, instead of returning a UTC time that could be mistaken for local
    if airport not in AIRPORT_TIMEZONES:
        return {"error": f"Unknown airport '{airport}'. Use the IATA code of an airport in the booking, e.g. FCO."}
    return {"airport": airport, "local_time": current_local_time(airport)}


GET_TIME_TOOL = {
    "type": "function",
    "function": {
        "name": "get_time",
        "description": (
            "Returns the current local date and time at an airport, as 'YYYY-MM-DD HH:MM'. "
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "airport_code": {
                    "type": "string",
                    "description": "IATA code of the airport, e.g. FCO.",
                },
            },
            "required": ["airport_code"],
        },
    },
}