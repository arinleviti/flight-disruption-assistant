from backend.models.case_state import Disruption
from backend.tools.get_booking import convert_offset_to_time
import json
from pathlib import Path
from datetime import datetime, timezone, timedelta

DATA_PATH = Path(__file__).resolve().parent.parent.parent.parent / "data" / "disruptions.json"



def get_disruption(flight_no: str) -> dict:
    number = flight_no.strip().upper()
    if not number:
        return {"error": "The flight number is empty. Ask the passenger for it."}
    records = json.loads(DATA_PATH.read_text(encoding="utf-8"))
    flight = None
    for item in records:
        if item["flight_no"] == number:
            flight = item
            break
    if flight is None:
        return {"error": f"No disruption recorded for flight {number}. The flight is operating normally."}

     # Work on a copy of the raw data, and convert it to the shape Disruption expects
    disruption_data = dict(flight)

    # .pop() removes the key from the dictionary and returns its value
    disruption_data["announced_at"] = convert_offset_to_time(
        disruption_data.pop("announced_offset_minutes")
    )

    # Validate last, once the data has the right shape
    disruption = Disruption.model_validate(disruption_data)

    return disruption.model_dump(mode="json")

GET_DISRUPTION_TOOL = {
    "type": "function",
    "function": {
        "name": "get_disruption",
        "description": (
            "Get the airline's record of what happened to a flight: whether it was cancelled "
            "or delayed, when this was announced, the stated cause, and the expected delay "
            "in minutes. Call this right after get_booking, using the flight number from the "
            "booking. If it returns an error saying no disruption is recorded, the flight is "
            "operating normally: tell the passenger politely that this assistant handles "
            "cancellations and delays only."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "flight_no": {
                    "type": "string",
                    "description": "The flight number from the booking, e.g. AU610.",
                },
            },
            "required": ["flight_no"],
        },
    },
}