from backend.models.case_state import OriginalBooking, Passenger
import json
import hashlib
from pathlib import Path

DATA_PATH = Path(__file__).resolve().parent.parent.parent.parent / "data" / "bookings.json"

def pick_record(ref: str, records: list[dict]) -> dict:
    for record in records:
        if record["booking"]["booking_ref"] == ref:
            return record
    digest = hashlib.sha256(ref.encode("utf-8")).hexdigest()
    return records[int(digest, 16) % len(records)]
    

def get_booking(booking_ref: str) -> dict:
    ref = booking_ref.strip().upper()
    if not ref:
        return {"error": "The booking reference is empty. Ask the passenger for it."}
    #read_text() reads the file into a string, and json.loads() converts that JSON string into native Python objects.
    
    records = json.loads(DATA_PATH.read_text(encoding="utf-8"))
    record = pick_record(ref, records)

    #pydantic models can't take a dictionary directly, it only accepts named arguments Passenger(id="P-001", name="Marco Rossi", contact="...", special_needs=[])
    # with model_validate you can pass a whole object
    passenger = Passenger.model_validate(record["passenger"])
   
    booking_data = dict(record["booking"])     # copy the booking
    booking_data["booking_ref"] = ref          # use the reference the visitor typed
    booking = OriginalBooking.model_validate(booking_data)

    return {
        "passenger": passenger.model_dump(mode="json"),
        "booking": booking.model_dump(mode="json"),
    }


GET_BOOKING_TOOL = {
    "type": "function",
    "function": {
        "name": "get_booking",
        "description": (
            "Look up a passenger's booking by its booking reference. Returns the passenger's "
            "details and the original flight (flight number, route, scheduled times, distance). "
            "Call this as soon as the passenger gives you a booking reference, before discussing "
            "rebooking, care or compensation. If it returns an error, ask the passenger to check "
            "the reference. Never guess or invent booking details."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "booking_ref": {
                    "type": "string",
                    "description": "The booking reference exactly as the passenger gave it.",
                },
            },
            "required": ["booking_ref"],
        },
    },
}