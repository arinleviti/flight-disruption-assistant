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
    expected_delay_minutes: float | None = None


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


# ---------- Compensation (filled by the compensation agent) ----------

class Compensation(BaseModel):
    eligible: bool
    amount_eur: int
    is_extraordinary: bool
    reasoning: str
    sources: list[str] = Field(default_factory=list)  # passages retrieved by RAG


# ---------- Care (filled by the care tools) ----------

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
    vouchers_issued: list[Voucher] = Field(default_factory=list)
    hotel_booked: HotelBooking | None = None


# ---------- Escalation ----------

class Escalation(BaseModel):
    reason: str
    at: datetime


# ---------- The case state itself ----------

class CaseState(BaseModel):
    case_id: str
    status: Literal["open", "escalated", "closed"] = "open"
    passenger: Passenger
    original_booking: OriginalBooking
    disruption: Disruption
    rebooking: Rebooking = Field(default_factory=Rebooking)
    compensation: Compensation | None = None
    care: Care = Field(default_factory=Care)
    escalation: Escalation | None = None