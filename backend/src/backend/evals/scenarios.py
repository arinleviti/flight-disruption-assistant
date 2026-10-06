"""The eval scenarios: one scripted passenger per demo booking, and what must be true at the end.

Each scenario is a list of messages the "passenger" sends, one per turn, plus the expected
outcome. The messages are written so a correct assistant can finish the case with them:
the passenger gives every preference up front, then accepts the recommended flight,
confirms, and says goodbye.

The expectations are checked in code against the case file (see run_evals.py), never by
reading the assistant's wording.
"""

from dataclasses import dataclass, field


@dataclass
class Scenario:
    ref: str                     # the booking reference
    title: str                   # a short description, shown in the report
    messages: list[str]          # what the passenger says, one message per turn

    # What must be true at the end. None means "don't check this".
    has_disruption: bool = True
    rebooked: bool | None = None            # True: a new flight was booked. False: kept the original flight
    is_extraordinary: bool | None = None    # the compensation agent's legal judgment
    eligible: bool | None = None            # compensation owed or not
    amounts_eur: set[int] = field(default_factory=set)   # allowed amounts (empty: don't check)
    care_expected: bool = True              # compute_care_entitlements must have run
    closed: bool = True                     # close_case must have closed the case
    direct_only: bool = False               # the booked flight must be direct (no "via")
    not_before_original_date: bool = False  # the booked flight can't leave before the original date


# The usual messages for a cancellation: preferences in one go, accept, confirm, goodbye
def cancellation_script(ref: str, preferences: str) -> list[str]:
    return [
        f"Hi, my flight has been cancelled. My booking reference is {ref}.",
        preferences,
        "I'll take the option you recommend.",
        "Yes, please book it.",
        "Thank you, that's everything.",
    ]


# The usual messages for a delay the passenger accepts
def delay_script(ref: str) -> list[str]:
    return [
        f"Hi, my flight is delayed. My booking reference is {ref}.",
        "I'll keep my flight, thanks.",
        "Thank you, that's everything.",
    ]


SCENARIOS = [
    Scenario(
        ref="AZX4K2",
        title="Marco · technical fault, cancelled",
        messages=cancellation_script(
            "AZX4K2",
            "The earliest available flight please. Connections are fine and I don't need any assistance.",
        ),
        rebooked=True,
        is_extraordinary=False,            # technical problems are not extraordinary (Wallentin-Hermann)
        eligible=True,
        amounts_eur={250, 125},            # 1,105 km: €250, or €125 if the new flight arrives within 2 h
    ),
    Scenario(
        ref="BRT9Q7",
        title="Sophie · storm, delayed 5 h",
        messages=delay_script("BRT9Q7"),
        rebooked=False,
        is_extraordinary=True,             # severe weather + ATC suspending departures
        eligible=False,
        amounts_eur={0},
    ),
    Scenario(
        ref="KMW3P8",
        title="Lukas · own crew strike, wheelchair",
        messages=cancellation_script(
            "KMW3P8",
            "A direct flight please, as early as possible. Yes, I still need wheelchair assistance.",
        ),
        rebooked=True,
        is_extraordinary=False,            # strike by the airline's own staff (Krüsemann, Airhelp v SAS)
        eligible=True,
        amounts_eur={600, 300},            # 6,880 km: €600, or €300 if the new flight arrives within 4 h
        direct_only=True,
    ),
    Scenario(
        ref="GBX7T2",
        title="Giulia · schedule change, 10 days ahead",
        messages=cancellation_script(
            "GBX7T2",
            "A flight on the same day as my original one please, as close as possible to the original time. "
            "Direct only, and I don't need any assistance.",
        ),
        rebooked=True,
        is_extraordinary=False,            # a commercial decision is the airline's own choice
        # The amount depends on which re-route is chosen (7–13 days' notice), so it isn't checked
        not_before_original_date=True,
    ),
    Scenario(
        ref="TMQ4L9",
        title="Thomas · schedule change, 20 days ahead",
        messages=cancellation_script(
            "TMQ4L9",
            "A flight on the same day as my original one please, any time is fine. "
            "Direct only, and I don't need any assistance.",
        ),
        rebooked=True,
        is_extraordinary=False,
        eligible=False,
        amounts_eur={0},                   # 14+ days' notice: nothing owed (Art. 5(1)(c))
        not_before_original_date=True,     # the bug we saw: offering flights 20 days early
    ),
    Scenario(
        ref="ACR5N8",
        title="Ana · French ATC strike",
        messages=cancellation_script(
            "ACR5N8",
            "The earliest available flight please. Connections are fine and I don't need any assistance.",
        ),
        rebooked=True,
        is_extraordinary=True,             # a strike by people who don't work for the airline
        eligible=False,
        amounts_eur={0},
        not_before_original_date=True,
    ),
    Scenario(
        ref="ECW2H6",
        title="Emily · long-haul delay 4 h, inbound technical",
        messages=delay_script("ECW2H6"),
        rebooked=False,
        is_extraordinary=False,            # the earlier flight's cause was technical (TAP C-74/19)
        eligible=True,
        amounts_eur={600, 300},
    ),
    Scenario(
        ref="PGB3K1",
        title="Paolo · bird strike",
        messages=cancellation_script(
            "PGB3K1",
            "The earliest available flight please. Connections are fine and I don't need any assistance.",
        ),
        rebooked=True,
        is_extraordinary=True,             # bird strike (Pešková)
        eligible=False,
        amounts_eur={0},
    ),
    Scenario(
        ref="LFD8V4",
        title="Luca · no disruption",
        messages=[
            "Hi, I'm worried about my flight. My booking reference is LFD8V4.",
            "Ok, thanks.",
        ],
        has_disruption=False,              # nothing to rebook, assess or close
        rebooked=False,
        care_expected=False,
        closed=False,
    ),
]