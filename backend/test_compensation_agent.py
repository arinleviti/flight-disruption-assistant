import time
from datetime import datetime, timedelta, timezone

from dotenv import load_dotenv

load_dotenv()  # the agent calls the LLM, so it needs the API keys

from backend.agents.compensation_agent import compensation_agent
from backend.models.case_state import (
    CaseState,
    Disruption,
    FlightOption,
    OriginalBooking,
    Passenger,
)
from backend.rag.knowledge_base import build_knowledge_base

now = datetime.now(timezone.utc)


def make_case(distance_km, disruption_type, cause, delay_minutes=None, rebooked_late_by=None):
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
        stated_cause=cause, expected_delay_minutes=delay_minutes,
    )
    if rebooked_late_by is not None:
        case.rebooking.confirmed = FlightOption(
            flight_id="F-TEST", flight_no="AU999", origin="FCO", destination="CDG",
            departure=departure + rebooked_late_by, arrival=arrival + rebooked_late_by,
        )
    return case


# (label, case, expected is_extraordinary)
scenarios = [
    ("Marco: hydraulic fault",
     make_case(1105, "cancellation", "Technical fault: hydraulic system issue found during the pre-flight inspection. Aircraft grounded for repairs.", rebooked_late_by=timedelta(hours=20)),
     False),
    ("Sophie: thunderstorms",
     make_case(1455, "delay", "Severe thunderstorms over Paris Charles de Gaulle. Air traffic control has suspended departures.", delay_minutes=300),
     True),
    ("Lukas: own cabin crew strike",
     make_case(6880, "cancellation", "Industrial action by Aurora Airways cabin crew. Insufficient crew available to operate the flight.", rebooked_late_by=timedelta(hours=22)),
     False),
    ("Ana: French ATC strike",
     make_case(1455, "cancellation", "Strike by French air traffic controllers. The authorities have ordered airlines to reduce departures from Paris airports.", rebooked_late_by=timedelta(hours=20)),
     True),
    ("Emily: late inbound aircraft (technical inspection)",
     make_case(6880, "delay", "Late arrival of the inbound aircraft, held for an unscheduled technical inspection at its previous airport.", delay_minutes=240),
     False),
    ("Paolo: bird strike",
     make_case(1105, "cancellation", "Bird strike on landing during the aircraft's previous flight. Aircraft grounded for a mandatory inspection.", rebooked_late_by=timedelta(hours=20)),
     True),
]

build_knowledge_base()

for label, case, expected in scenarios:
    print(f"\n=== {label} ===")
    result = compensation_agent(case)
    if "error" in result:
        print(f"ERROR  {result['error']}")
    else:
        status = "PASS" if result["is_extraordinary"] == expected else "FAIL"
        print(f"{status}  extraordinary={result['is_extraordinary']} (expected {expected}), amount={result['amount_eur']} EUR")
        print(f"      {result['reasoning']}")
        print(f"      sources: {result['sources']}")
    time.sleep(20)  # give Groq's per-minute token limit time to recover between cases