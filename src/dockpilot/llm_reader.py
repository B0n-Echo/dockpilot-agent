"""Read a driver's message with an AI model (OpenRouter); fall back to the rules on any problem.

    uv run python -m dockpilot.llm_reader "Stuck in traffic, ETA 6:30"
"""

import json
import os
import re
import sys
import urllib.request
from dataclasses import replace
from datetime import timedelta
from functools import partial

from dockpilot import paths, trace
from dockpilot.clock import SystemClock
from dockpilot.reader import INTENTS, REASONS, Reading, read_message

URL = "https://openrouter.ai/api/v1/chat/completions"
"""OpenRouter's chat-completions endpoint."""

ALLOWED_INTENTS = {name for name, _ in INTENTS} | {"GENERAL_QUESTION", "UNKNOWN", "CONFIRM", "DECLINE"}
ALLOWED_REASONS = {code for code, _ in REASONS} | {"ROUTE_ISSUE", "LATE_DEPARTURE", "OTHER"}

INSTRUCTION = f"""You extract fields from a truck driver's chat message to a warehouse. You only read; you never reply.
Return JSON only, no other text, with exactly these keys:
intent: one of {sorted(ALLOWED_INTENTS)}
eta: when the driver says they will arrive, 24-hour "HH:MM", or null
eta_confidence: "HIGH" for an exact time, "MEDIUM" for "around/about", "LOW" if only a delay is given, or null
delay_minutes: integer minutes late if stated, or null
delay_reason: one of {sorted(ALLOWED_REASONS)} or null
not_before: earliest slot time the driver asks for, "HH:MM" or null
leave_by: time the driver must leave by, "HH:MM" or null
urgent: true or false
shipment_ref: a shipment id like "SHP1014" if written, or null
Rules: times are on the day the message was sent and are not earlier than the sent time unless clearly said.
Never turn a relative delay ("late by one hour") into a clock time: leave eta null and set eta_confidence "LOW"."""
"""The fixed instruction sent with every message."""


def load_env(path=paths.REPO_ROOT / ".env"):
    """Settings from the git-ignored .env file (KEY=VALUE lines); real environment variables win."""
    values = {}
    if path.exists():
        for line in path.read_text().splitlines():
            if "=" in line and not line.lstrip().startswith("#"):
                key, _, value = line.partition("=")
                values[key.strip()] = value.strip().strip('"').strip("'")
    values.update({k: v for k, v in os.environ.items() if k.startswith("OPENROUTER_")})
    return values


def _call_openrouter(key, model, messages):
    """One request to OpenRouter (10 s limit); returns the reply text. The key is never logged."""
    body = json.dumps({"model": model, "messages": messages, "response_format": {"type": "json_object"}}).encode()
    req = urllib.request.Request(URL, data=body, headers={
        "Authorization": f"Bearer {key}", "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=10) as resp:
        return json.loads(resp.read())["choices"][0]["message"]["content"]


def _time(value, received_at):
    """Turn "HH:MM" into a datetime on the message's day (next day if more than an hour before it)."""
    if value is None:
        return None
    m = re.fullmatch(r"([01]?\d|2[0-3]):([0-5]\d)", str(value))
    if not m:
        raise ValueError(f"bad time {value!r}")
    t = received_at.replace(hour=int(m.group(1)), minute=int(m.group(2)), second=0, microsecond=0)
    return t + timedelta(days=1) if t < received_at - timedelta(hours=1) else t


def _validate(data, received_at):
    """Check the AI's JSON field by field and build a Reading; ValueError if anything is off-list."""
    intent, conf, reason = data["intent"], data.get("eta_confidence"), data.get("delay_reason")
    delay, ref = data.get("delay_minutes"), data.get("shipment_ref")
    if intent not in ALLOWED_INTENTS or conf not in (None, "HIGH", "MEDIUM", "LOW") or (
            reason is not None and reason not in ALLOWED_REASONS):
        raise ValueError("value not in the allowed lists")
    if delay is not None and (not isinstance(delay, int) or delay < 0):
        raise ValueError("bad delay")
    if ref is not None and not re.fullmatch(r"SHP\d{4}", str(ref)):
        raise ValueError("bad shipment ref")
    return Reading(
        intent=intent, eta=_time(data.get("eta"), received_at), eta_confidence=conf, delay_minutes=delay,
        delay_reason=reason, not_before=_time(data.get("not_before"), received_at),
        leave_by=_time(data.get("leave_by"), received_at), urgent=bool(data.get("urgent")), shipment_ref=ref,
        needs_clarification=intent == "REPORT_DELAY" and conf in ("LOW", None), read_by="ai",
    )


def read_message_ai(text, received_at, call=None):
    """Read a message with the AI if a key and model are set; on any problem return the rules' Reading.

    call: optional stand-in for the network (tests pass a fake). The rules' "ask first" wins only when they saw
    a relative delay and no clock time ("late by one hour"); otherwise the AI's reading stands.
    """
    rules = read_message(text, received_at)
    model = "test stand-in"
    if call is None:
        env = load_env()
        key, model = env.get("OPENROUTER_API_KEY"), env.get("OPENROUTER_MODEL")
        if not (key and model):
            trace.note("rules used: no AI key or model in .env")
            return rules
        call = partial(_call_openrouter, key, model)
    messages = [{"role": "system", "content": INSTRUCTION},
                {"role": "user", "content": f"Sent at {received_at:%H:%M}: {text}"}]
    try:
        with trace.span(f"AI model call ({model})", "ai", f"[system] {INSTRUCTION}\n[user] {messages[1]['content']}") as ai:
            raw = call(messages)
            ai.out = raw
        raw = re.sub(r"^```(?:json)?\s*|\s*```$", "", raw.strip())
        reading = _validate(json.loads(raw), received_at)
    except TimeoutError:
        trace.note("rules used: AI timed out")
        return rules
    except OSError as exc:
        trace.note(f"rules used: AI unreachable ({type(exc).__name__})")
        return rules
    except (ValueError, KeyError, TypeError, IndexError) as exc:
        trace.note(f"rules used: AI answer invalid ({exc})"[:160])
        return rules
    caution = rules.needs_clarification and rules.delay_minutes is not None and rules.eta is None
    return replace(reading, needs_clarification=reading.needs_clarification or caution)


if __name__ == "__main__":
    r = read_message_ai(" ".join(sys.argv[1:]) or "Stuck in traffic, ETA 6:30", SystemClock().now())
    for field, value in r.__dict__.items():
        print(f"{field:20} {value:%H:%M}" if hasattr(value, "hour") else f"{field:20} {value}")
