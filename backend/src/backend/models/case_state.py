from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field


# ---------- Passenger & booking (loaded from the database at the start) ----------

class Passenger(BaseModel):
    id: str
    name: str
    contact: str
    special_needs: list[str] = Field(default_factory=list)


class OriginalBooking(BaseModel):
    booking_ref: str
    flight_no: str
    origin: str               # airport code, e.g. "FCO"
    destination: str          # airport code, e.g. "CDG"
    scheduled_departure: datetime
    scheduled_arrival: datetime
    distance_km: int


class Disruption(BaseModel):
    flight_no: str
    type: Literal["cancellation", "delay"]
    announced_at: datetime
    stated_cause: str | None = None          # from the airline's disruption record
    expected_delay_minutes: int | None = None


# ---------- Rebooking (filled by the rebooking agent + passenger's choice) ----------

class FlightOption(BaseModel):
    flight_id: str
    flight_no: str
    origin: str
    destination: str
    via: str | None = None   # connection airport, if any
    departure: datetime
    arrival: datetime


class Rebooking(BaseModel):
    options_offered: list[FlightOption] = Field(default_factory=list)
    rejected_count: int = 0
    confirmed: FlightOption | None = None


class RebookingResult(BaseModel):
    """The shape of the rebooking agent's final answer, as described in its prompt."""
    options: list[FlightOption]
    recommended_flight_id: str | None
    reason: str


# ---------- Compensation (filled by the compensation agent) ----------

class Compensation(BaseModel):
    eligible: bool
    amount_eur: int
    is_extraordinary: bool
    reasoning: str
    sources: list[str] = Field(default_factory=list)  # passages retrieved by RAG


# ---------- Care (filled by compute_care_entitlements) ----------

class CareEntitlements(BaseModel):
    meals: bool
    hotel_nights: int
    transport: bool
    communications: int


class Voucher(BaseModel):
    voucher_id: str
    type: Literal["meal", "transport"]
    amount_eur: int


class HotelBooking(BaseModel):
    booking_id: str
    hotel_name: str
    nights: int


class Care(BaseModel):
    entitlements: CareEntitlements | None = None
    meal_vouchers: int = 0              # how many meal vouchers the passenger is owed
    meal_voucher_eur: int = 0           # value of each meal voucher at the departure airport
    transport_voucher_eur: int = 0      # value of the transport voucher, if transport is owed
    vouchers_issued: list[Voucher] = Field(default_factory=list)
    hotel_booked: HotelBooking | None = None


# ---------- Escalation ----------

class Escalation(BaseModel):
    reason: str
    at: datetime


# ---------- The case file itself ----------

class CaseState(BaseModel):
    """Everything known about one booking's case, kept for the whole conversation.

    It starts almost empty and fills up as tools return results:
    booking -> disruption -> options offered -> confirmed flight -> care -> compensation
    """
    case_id: str
    status: Literal["open", "escalated", "closed"] = "open"
    passenger: Passenger | None = None
    original_booking: OriginalBooking | None = None
    disruption: Disruption | None = None
    rebooking: Rebooking = Field(default_factory=Rebooking)
    compensation: Compensation | None = None
    care: Care = Field(default_factory=Care)
    escalation: Escalation | None = None