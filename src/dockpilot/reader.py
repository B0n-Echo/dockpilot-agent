"""Read a driver's message into fields, with plain rules. Reading only: no decisions here."""

import re
from dataclasses import dataclass
from datetime import timedelta

TIME = re.compile(r"\b(\d{1,2})(?::(\d{2}))?\s*(am|pm)?\b")
"""A clock time: "11:10", "6:30", "1 PM", or a bare hour (used only after "after" / "before")."""

DELAY = re.compile(r"\b(\d+|one|two|three|half an?|an?)\s*(hours?|hrs?|minutes?|mins?)\b")
"""A relative delay such as "one hour", "45 minutes", "half an hour"."""

WORDS = {"one": 1, "two": 2, "three": 3, "a": 1, "an": 1, "half a": 0.5, "half an": 0.5}
"""Number words used in delays."""

YES = re.compile(r"^(yes|yeah|yep|ok|okay|sure|confirm|go ahead|please book|book it)\b")
NO = re.compile(r"^(no|nope|cancel|not now|don'?t)\b")

INTENTS = [  # first match wins
    ("EARLY_ARRIVAL", r"already at the gate|\bearly\b"),
    ("CHECK_STATUS", r"still active|been assigned|\bstatus\b"),
    ("REPORT_DELAY", r"\blate\b|delay|\beta\b|traffic|tyre|tire|breakdown|repaired|missed"),
    ("ASK_SLOT_OPTIONS", r"\bslots?\b|options"),
]
"""Intent rules, checked in order."""

REASONS = [("TRAFFIC", r"traffic"), ("BREAKDOWN", r"tyre|tire|breakdown|puncture|repair"),
           ("LOADING_DELAY", r"\borigin\b|loading|released"), ("WEATHER", r"weather|rain|fog")]
"""Delay reasons, using the client's eta_updates codes."""


@dataclass(frozen=True)
class Reading:
    """What a message says: intent, arrival time and how sure, delay, reason, limits, urgency, shipment,
    and which reader produced it (read_by: "rules" or "ai")."""

    intent: str
    eta: object = None
    eta_confidence: str = None
    delay_minutes: int = None
    delay_reason: str = None
    not_before: object = None
    leave_by: object = None
    urgent: bool = False
    shipment_ref: str = None
    needs_clarification: bool = False
    read_by: str = "rules"


def _next_occurrence(hour, minute, ampm, received_at):
    """The first time at hour:minute on or after the message; "6:30" at 16:24 means 18:30."""
    if ampm:
        hours = [hour % 12 + (12 if ampm == "pm" else 0)]
    else:
        hours = [hour % 12, hour % 12 + 12]
    for h in sorted(hours):
        t = received_at.replace(hour=h, minute=minute, second=0, microsecond=0)
        if t >= received_at:
            return t
    return received_at.replace(hour=hours[-1], minute=minute, second=0, microsecond=0) + timedelta(days=1)


def read_message(text, received_at):
    """Turn a driver's message, sent at received_at, into a Reading."""
    t = text.lower().strip()
    if len(t.split()) <= 5 and YES.match(t):
        return Reading("CONFIRM")
    if len(t.split()) <= 5 and NO.match(t):
        return Reading("DECLINE")

    eta = not_before = leave_by = None
    confidence = None
    for sentence in re.split(r"[.!?]", t):
        last = 0
        for m in TIME.finditer(sentence):
            before = sentence[last:m.start()]
            last = m.end()
            hour, minute, ampm = int(m.group(1)), int(m.group(2) or 0), m.group(3)
            bare = m.group(2) is None and ampm is None
            if hour > 23 or (bare and not re.search(r"\b(after|before)\s*$", before)):
                continue
            when = _next_occurrence(hour, minute, ampm, received_at)
            if re.search(r"\b(eta|reach|reaching|arrive|arriving)\b", before) and eta is None:
                eta = when
                confidence = "MEDIUM" if re.search(r"around|about|approx|roughly|maybe", before) else "HIGH"
            elif "leave" in before and "before" in before:
                leave_by = when
            elif re.search(r"\bafter\s*$", before):
                not_before = when

    delay = None
    d = DELAY.search(t)
    if d:
        n = WORDS.get(d.group(1), None) if not d.group(1).isdigit() else int(d.group(1))
        delay = int(n * (60 if d.group(2).startswith(("hour", "hr")) else 1))
        if eta is None:
            confidence = "LOW"

    intent = next((name for name, rule in INTENTS if re.search(rule, t)), None)
    intent = intent or ("GENERAL_QUESTION" if "?" in t else "UNKNOWN")
    ref = re.search(r"\bshp\d{4}\b", t)
    return Reading(
        intent=intent, eta=eta, eta_confidence=confidence, delay_minutes=delay,
        delay_reason=next((code for code, rule in REASONS if re.search(rule, t)), None),
        not_before=not_before, leave_by=leave_by,
        urgent=bool(re.search(r"urgent|asap|first possible", t)),
        shipment_ref=ref.group(0).upper() if ref else None,
        needs_clarification=intent == "REPORT_DELAY" and confidence in ("LOW", None),
    )
