"""The conversation agent (dp-14): talks with a logged-in driver; every decision is made by the earlier blocks."""

import json
import re
import uuid
from dataclasses import dataclass, replace
from datetime import datetime, timedelta

from dockpilot.allocation import request_slot
from dockpilot.auth import LIVE, choose_shipment, get_session
from dockpilot.bookings import convert_hold
from dockpilot.clock import to_iso
from dockpilot.escalations import SHORT, create_escalation, deliver, escalate_if_no_options
from dockpilot.feasibility import latest_eta
from dockpilot.holds import expire_holds, get_hold
from dockpilot.llm_reader import read_message_ai
from dockpilot.reader import DELAY, _next_occurrence
from dockpilot.settings import SETTINGS

BOOKING_INTENTS = ("REPORT_DELAY", "ASK_SLOT_OPTIONS", "EARLY_ARRIVAL")
"""Intents that lead to slot options."""

THREAD_INTENTS = BOOKING_INTENTS + ("CHECK_STATUS", "GENERAL_QUESTION")
"""Intents the client's chat_threads table accepts (others are stored as UNKNOWN)."""

SAFETY = re.compile(r"accident|injur|hurt|fire|emergency|unsafe|robbed|police")
"""Words that go straight to a coordinator."""

BARE_TIME = re.compile(r"^\s*(?:around|about|approx\.?|maybe|by|at|reach(?:ing)? (?:by|at)?)?\s*(\d{1,2})(?:[:.](\d{2}))?\s*(am|pm)?\s*\.?\s*$")
"""A reply that is just a time, like "11:15" or "around 11:15"."""

STATUS = {"SHIPMENT": "WAITING_FOR_DRIVER", "ETA": "WAITING_FOR_DRIVER", "CHOICE": "WAITING_FOR_DRIVER",
          "WAREHOUSE": "WAITING_FOR_WAREHOUSE", "NOTHING": "OPEN"}
"""Thread status for what the agent waits for."""

GENERAL = "I can help with delays, arrival times and dock slots. When do you expect to arrive?"
CLOSE_TEXT = ("I haven't heard back, so I've closed this chat. Your current booking is unchanged. "
              "Message me any time to start again.")


@dataclass(frozen=True)
class Reply:
    """What the agent says, plus what happened: thread, state, options, hold, booking, escalation."""

    ok: bool
    text: str
    thread_id: str | None = None
    state: str | None = None
    options: tuple = ()
    hold_group: str | None = None
    appointment_id: str | None = None
    escalation_id: str | None = None


def _id(prefix):
    """A fresh unique id such as "DPT-3f9a1c2b7d4e"."""
    return f"{prefix}-{uuid.uuid4().hex[:12]}"


def _hm(ts):
    """HH:MM of a datetime or ISO text."""
    return (datetime.fromisoformat(ts) if isinstance(ts, str) else ts).strftime("%H:%M")


def _post(conn, thread, sender, ref, text, clock, reading=None, external=None, intent=None):
    """Save one message in the client's chat_messages table."""
    conn.execute(
        """INSERT INTO chat_messages (chat_message_id, thread_id, sender_type, sender_reference, message_text, message_ts,
               external_message_id, parsed_intent, extracted_eta_ts, requires_human_review)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (mid := _id("DPM"), thread, sender, ref, text, to_iso(clock.now()), external,
         reading.intent if reading else intent, to_iso(reading.eta) if reading and reading.eta else None,
         int(bool(reading and reading.needs_clarification))))
    return mid


def _state(conn, thread):
    """(what the agent waits for, its note) for a thread."""
    row = conn.execute("SELECT awaiting, note FROM conversation_state WHERE thread_id = ?", (thread,)).fetchone()
    return (row["awaiting"], json.loads(row["note"])) if row else ("NOTHING", {})


def _set_state(conn, thread, awaiting, note, clock):
    """Remember what the agent waits for; every new question restarts the silence timer."""
    now = to_iso(clock.now())
    conn.execute(
        """INSERT INTO conversation_state (thread_id, awaiting, note, times_asked, asked_at, updated_at)
           VALUES (?, ?, ?, 1, ?, ?)
           ON CONFLICT(thread_id) DO UPDATE SET note = excluded.note, asked_at = excluded.asked_at, nudged_at = NULL,
               times_asked = CASE WHEN awaiting = excluded.awaiting THEN times_asked + 1 ELSE 1 END,
               awaiting = excluded.awaiting, updated_at = excluded.updated_at""",
        (thread, awaiting, json.dumps(note), now, now))


def _finish(conn, thread, reply, clock, external=None, status=None):
    """Save the agent's reply and set the thread status."""
    _post(conn, thread, "AGENT", "dockpilot", reply.text, clock, external=external, intent=reply.state)
    status = status or ("ESCALATED" if reply.escalation_id else STATUS[reply.state])
    conn.execute("UPDATE chat_threads SET thread_status = ? WHERE thread_id = ?", (status, thread))
    return replace(reply, thread_id=thread)


def _plan(conn, shipment):
    """The shipment's planned arrival."""
    return datetime.fromisoformat(conn.execute(
        "SELECT original_eta_ts FROM shipments WHERE shipment_id = ?", (shipment,)).fetchone()[0])


def _bare_time(text, now):
    """(time, confidence) when the whole message is a time like "around 11:15"; else None."""
    m = BARE_TIME.match(text.lower())
    if not m or not (m.group(2) or m.group(3)) or int(m.group(1)) > 23:
        return None
    sure = "MEDIUM" if re.match(r"\s*(around|about|approx|maybe)", text.lower()) else "HIGH"
    return _next_occurrence(int(m.group(1)), int(m.group(2) or 0), m.group(3), now), sure


def _save_eta(conn, shipment, eta, confidence, reason, text, clock):
    """Record the driver's arrival time in the client's eta_updates table (DRIVER_DECLARED)."""
    # "ETA_DP-" sorts after the client's "ETA0…" ids, so ours wins a same-second tie in latest_eta.
    conn.execute(
        """INSERT INTO eta_updates (eta_update_id, shipment_id, source_type, reported_by_driver_id, declared_eta_ts,
               confidence_code, delay_reason_code, note, created_at)
           SELECT ?, shipment_id, 'DRIVER_DECLARED', driver_id, ?, ?, ?, ?, ? FROM shipments WHERE shipment_id = ?""",
        (_id("ETA_DP"), to_iso(eta), confidence or "MEDIUM", reason, text[:200], to_iso(clock.now()), shipment))


def _why_not(checked):
    """The three most common reasons the checked slots failed, in plain words."""
    counts = {}
    for f in checked:
        for r in f.reasons:
            counts[r.code] = counts.get(r.code, 0) + 1
    return ", ".join(SHORT.get(c, c) for c, _ in sorted(counts.items(), key=lambda kv: -kv[1])[:3]) or "no slots left today"


def _offer(conn, shipment, thread, key, clock, settings, intro=""):
    """Find options: hold the best and list up to 3, or escalate and tell the driver why nothing fits (D11)."""
    search, esc = escalate_if_no_options(conn, shipment, clock, settings, thread_id=thread)
    if esc:
        _set_state(conn, thread, "NOTHING", {}, clock)
        eta = latest_eta(conn, shipment, clock)[0]
        return Reply(True, f"{intro}No slot fits an arrival at {eta:%H:%M}: {_why_not(search.checked)}. "
                           "I've passed this to the coordinator; they'll call you.",
                     state="NOTHING", escalation_id=esc.escalation_id)
    own = _own_booking(conn, shipment)
    if own and search.options[0].slot_ids == own[1]:  # the best fit is the booking the truck already has (dp-19)
        o, eta = search.options[0], latest_eta(conn, shipment, clock)[0]
        fits = f"{intro}Good news: your booking {o.dock_code} {o.start:%H:%M}–{o.end:%H:%M} still works for {eta:%H:%M}."
        if own[2] != "PENDING_CONFIRMATION":
            _set_state(conn, thread, "NOTHING", {}, clock)
            return Reply(True, f"{fits} It's confirmed.", state="NOTHING")
        _set_state(conn, thread, "WAREHOUSE", {"appointment": own[0], "slot": f"{o.dock_code} {o.start:%H:%M}"}, clock)
        return Reply(True, f"{fits} It's waiting for the warehouse to confirm; I'll tell you as soon as they do.",
                     state="WAREHOUSE", appointment_id=own[0])
    opts = [{"slots": o.slot_ids, "dock": o.dock_code, "start": to_iso(o.start), "end": to_iso(o.end)}
            for o in search.options if not own or o.slot_ids != own[1]]
    return _hold_and_list(conn, shipment, thread, opts, 0, key, clock, settings, intro)


def _own_booking(conn, shipment):
    """The truck's live booking as (appointment id, its slot ids in time order, status), or None (dp-19)."""
    a = conn.execute(f"SELECT appointment_id, slot_id, appointment_status FROM appointments WHERE shipment_id = ? "
                     f"AND appointment_status IN {LIVE} ORDER BY is_current DESC, booked_at DESC LIMIT 1",
                     (shipment,)).fetchone()
    if not a:
        return None
    slots = {a["slot_id"]} | {r[0] for r in conn.execute("SELECT slot_id FROM slot_holds WHERE appointment_id = ?",
                                                          (a["appointment_id"],))}
    ordered = [r[0] for r in conn.execute(f"SELECT slot_id FROM appointment_slots WHERE slot_id IN "
                                          f"({','.join('?' * len(slots))}) ORDER BY slot_start_ts", tuple(slots))]
    return a["appointment_id"], ordered, a["appointment_status"]


def _waiting(conn, shipment, thread, note, clock):
    """Any message while the warehouse decides: "I'm still checking …" (dp-19); the status if they already answered."""
    a = conn.execute("SELECT appointment_status FROM appointments WHERE appointment_id = ?",
                     (note.get("appointment"),)).fetchone()
    if not a or a[0] != "PENDING_CONFIRMATION":
        _set_state(conn, thread, "NOTHING", {}, clock)
        return _status(conn, shipment, clock, "NOTHING")
    on_it = "; a coordinator is on it" if note.get("escalated") else ""
    return Reply(True, f"I'm still checking {note.get('slot', 'your slot')} with the warehouse{on_it}. "
                       "I'll get back to you as soon as they answer.", state="WAREHOUSE")


def _thread_for(conn, appointment_id):
    """The chat a booking came from: via its hold, or a chat waiting on it."""
    row = conn.execute("SELECT thread_id FROM slot_holds WHERE appointment_id = ? AND thread_id IS NOT NULL LIMIT 1",
                       (appointment_id,)).fetchone() or conn.execute(
        "SELECT thread_id FROM conversation_state WHERE json_extract(note, '$.appointment') = ? "
        "ORDER BY updated_at DESC LIMIT 1", (appointment_id,)).fetchone()
    return row and row[0]


def _hold_and_list(conn, shipment, thread, opts, pick, key, clock, settings, intro=""):
    """Ask for option `pick` (or the next free one if it was just taken) through the D7 policy (dp-10), then list the
    others; in a rush the driver waits for the collection window instead."""
    for j in [pick] + [k for k in range(len(opts)) if k != pick]:
        res = request_slot(conn, shipment, opts[j]["slots"], f"{key}:{j}", clock, settings, thread_id=thread)
        if res.status in ("HELD", "WAITING"):
            break
    else:
        _set_state(conn, thread, "NOTHING", {}, clock)
        return Reply(True, f"{intro}Those slots were just taken by other drivers. Please send your arrival time again.",
                     state="NOTHING")
    if res.status == "WAITING":
        o = opts[j]
        _set_state(conn, thread, "CHOICE", {"options": opts, "held": j, "hold": None, "request": res.request_id}, clock)
        return Reply(True, f"{intro}It's busy right now: I'm checking who gets {o['dock']} {_hm(o['start'])} and will "
                           "tell you within a minute.", state="CHOICE", options=tuple(opts))
    extra = " I've also asked a coordinator whether an earlier slot can be freed for you." if res.alert else ""
    return _held_reply(conn, thread, opts, j, res.hold, clock, intro, extra)


def _held_reply(conn, thread, opts, j, hold, clock, intro="", extra=""):
    """Remember the held option and tell the driver: "You can make D2 12:00–13:00 (held …). Reply 1 to book it…"."""
    o = opts[j]
    others = "".join(f", {k + 1} for {p['dock']} {_hm(p['start'])}" for k, p in enumerate(opts) if k != j)
    _set_state(conn, thread, "CHOICE", {"options": opts, "held": j, "hold": hold.group_id}, clock)
    return Reply(True, f"{intro}You can make {o['dock']} {_hm(o['start'])}–{_hm(o['end'])} (held for you until "
                       f"{hold.expires_at:%H:%M}). Reply {j + 1} to book it{others}.{extra}",
                 state="CHOICE", options=tuple(opts), hold_group=hold.group_id)


def _ask_eta(conn, shipment, thread, r, clock):
    """One clarifying question for a vague arrival (R7, DA-03); no hold yet."""
    now, plan = clock.now(), _plan(conn, shipment)
    guess = max(now, plan + timedelta(minutes=r.delay_minutes)) if r.delay_minutes is not None else None
    example = (guess or now + timedelta(hours=1)) + timedelta(minutes=15)
    example = _hm(example - timedelta(minutes=example.minute % 5))
    ask = f"Thanks. Your plan was {plan:%H:%M}. Will you reach around {guess:%H:%M}? " if guess else \
        "Thanks. When do you expect to arrive? "
    _set_state(conn, thread, "ETA", {"guess": guess and to_iso(guess), "reason": r.delay_reason, "example": example}, clock)
    return Reply(True, f"{ask}Reply with your arrival time, like {example}.", state="ETA")


def _status(conn, shipment, clock, awaiting):
    """The current booking and the latest arrival time, never shown as exact (D3)."""
    a = conn.execute(
        f"""SELECT a.appointment_status, sl.slot_start_ts, d.dock_code FROM appointments a
            JOIN appointment_slots sl ON sl.slot_id = a.slot_id JOIN docks d ON d.dock_id = sl.dock_id
            WHERE a.shipment_id = ? AND a.appointment_status IN {LIVE}
            ORDER BY a.is_current, a.booked_at DESC LIMIT 1""", (shipment,)).fetchone()
    eta, source, _ = latest_eta(conn, shipment, clock)
    words = {"PENDING_CONFIRMATION": "waiting for the warehouse to confirm", "CONFIRMED": "confirmed",
             "IN_PROGRESS": "unloading now"}
    booking = (f"Your booking: {a['dock_code']} at {_hm(a['slot_start_ts'])}, {words[a['appointment_status']]}. "
               if a else "You have no booking right now. ")
    origin = "from you" if source == "DRIVER_DECLARED" else "from the plan"
    return Reply(True, f"{booking}The arrival time we have for you is about {eta:%H:%M} ({origin}).", state=awaiting)


def _fresh(conn, shipment, thread, text, r, key, clock, settings):
    """A message with nothing pending: act on its intent."""
    now = clock.now()
    eta, conf = r.eta, r.eta_confidence
    if eta is None and (bare := _bare_time(text, now)):
        eta, conf = bare
    if r.intent == "EARLY_ARRIVAL" and eta is None:
        eta, conf = now, "HIGH"
    if r.intent == "CHECK_STATUS":
        _set_state(conn, thread, "NOTHING", {}, clock)
        return _status(conn, shipment, clock, "NOTHING")
    if eta is not None and conf != "LOW":
        _save_eta(conn, shipment, eta, conf, r.delay_reason, text, clock)
        return _offer(conn, shipment, thread, key, clock, settings)
    if r.intent == "ASK_SLOT_OPTIONS" and r.delay_minutes is None:
        return _offer(conn, shipment, thread, key, clock, settings)
    if r.intent in BOOKING_INTENTS or eta is not None:
        return _ask_eta(conn, shipment, thread, r, clock)
    _set_state(conn, thread, "NOTHING", {}, clock)
    return Reply(True, GENERAL, state="NOTHING")


def _answer_eta(conn, shipment, thread, text, r, note, key, clock, settings):
    """Answer to "what time?": a time, "yes" to our guess, or still vague -> best guess as LOW (R7)."""
    now = clock.now()
    eta, conf = (r.eta, r.eta_confidence) if r.eta and r.eta_confidence != "LOW" else (_bare_time(text, now) or (None, None))
    guess = datetime.fromisoformat(note["guess"]) if note.get("guess") else None
    if eta is None and r.intent == "CONFIRM" and guess:
        eta, conf = guess, "MEDIUM"
    if eta is None:
        if r.delay_minutes is not None:
            guess = max(now, _plan(conn, shipment) + timedelta(minutes=r.delay_minutes))
        eta, conf = guess or latest_eta(conn, shipment, clock)[0], "LOW"
    _save_eta(conn, shipment, eta, conf, r.delay_reason or note.get("reason"), text, clock)
    return _offer(conn, shipment, thread, key, clock, settings)


def _answer_choice(conn, shipment, thread, text, r, note, key, clock, settings):
    """Answer to the options: 1/yes books the held slot, 2/3 holds another, no lets the hold run out."""
    opts, held = note["options"], note["held"]
    if note.get("hold") is None:
        _set_state(conn, thread, "CHOICE", note, clock)
        return Reply(True, "Still deciding between the trucks that asked, one moment.", state="CHOICE")
    m = re.fullmatch(r"\s*(?:option\s*)?(\d)\s*\.?\s*", text.lower())
    n = int(m.group(1)) - 1 if m and 1 <= int(m.group(1)) <= len(opts) else (held if r.intent == "CONFIRM" else None)
    if n == held:
        res = convert_hold(conn, note["hold"], clock, settings)
        if not res.ok:
            return _offer(conn, shipment, thread, key, clock, settings, intro=f"{res.reasons[0].message} Here are fresh options. ")
        slot = f"{opts[held]['dock']} {_hm(opts[held]['start'])}"
        _set_state(conn, thread, "WAREHOUSE", {"appointment": res.appointment_id, "slot": slot}, clock)
        return Reply(True, f"Great, I'm confirming {slot} with the warehouse now. "
                           "I'll get back to you as soon as they answer.", state="WAREHOUSE", appointment_id=res.appointment_id)
    if n is not None:
        return _hold_and_list(conn, shipment, thread, opts, n, key, clock, settings)
    if r.intent == "DECLINE":
        _set_state(conn, thread, "NOTHING", {}, clock)
        return Reply(True, f"OK, I won't book it. The hold ends at {get_hold(conn, note['hold']).expires_at:%H:%M}. "
                           "Message me if you change your mind.", state="NOTHING")
    if r.eta or r.intent in BOOKING_INTENTS or _bare_time(text, clock.now()):
        return _fresh(conn, shipment, thread, text, r, key, clock, settings)
    _set_state(conn, thread, "CHOICE", note, clock)
    return Reply(True, f"Please reply with a number from 1 to {len(opts)} to pick a slot, or no.", state="CHOICE")


def _trip_label(t):
    """A trip in a few words, for "which one?"."""
    return {"WAITING": "now at the warehouse", "IN_DOCK": "now at the warehouse", "AT_GATE": "at the gate",
            "IN_TRANSIT": f"on the road, due {_hm(t.planned_eta)}"}.get(t.status, f"due {_hm(t.planned_eta)}")


def _match_trip(trips, text, asked):
    """Which trip the message names: shipment id, order number or its last digits, vehicle, or first/second."""
    up, flat = text.upper(), re.sub(r"\W", "", text.upper())
    for t in trips:
        if t.shipment_id in up or t.order_reference in up or re.sub(r"\W", "", t.vehicle_reg) in flat:
            return t.shipment_id
    rest = re.sub(r"\d{1,2}[:.]\d{2}", " ", DELAY.sub(" ", text.lower()))
    for n in re.findall(r"\b\d{2,4}\b", rest):
        for t in trips:
            if int(n) in (int(t.order_reference[-3:]), int(t.shipment_id[-4:])):
                return t.shipment_id
    if asked:
        words = {"1": 0, "first": 0, "2": 1, "second": 1, "3": 2, "third": 2}
        m = re.search(r"\b(first|second|third)\b", text.lower()) or re.fullmatch(r"\s*([123])\s*", text)
        if m and words[m.group(1)] < len(trips):
            return trips[words[m.group(1)]].shipment_id
    return None


def _pick_shipment(conn, s, thread, text, r, awaiting, note, key, clock, settings, reader):
    """Two trips and none chosen (D2): find it in the message, or ask which one; then handle the parked message."""
    pick = _match_trip(s.trips, text, awaiting == "SHIPMENT")
    if pick is None:
        names = [f"{t.order_reference} ({_trip_label(t)})" for t in s.trips]
        _set_state(conn, thread, "SHIPMENT", {"parked": note.get("parked") or text}, clock)
        return Reply(True, f"You have {len(names)} trips today: {', '.join(names[:-1])} or {names[-1]}. "
                           "Which one is this about?", state="SHIPMENT")
    choose_shipment(conn, s.session_id, pick, clock)
    conn.execute("UPDATE chat_threads SET shipment_id = ? WHERE thread_id = ?", (pick, thread))
    _set_state(conn, thread, "NOTHING", {}, clock)
    parked = note.get("parked") if awaiting == "SHIPMENT" else None
    if parked and not r.eta:
        text, r = parked, reader(parked, clock.now())
    return _fresh(conn, pick, thread, text, r, key, clock, settings)


def _safety(conn, s, thread, text, clock):
    """Safety words: a SAFETY escalation now."""
    esc = create_escalation(conn, "SAFETY", s.shipment_id or s.trips[0].shipment_id, clock, thread_id=thread,
                            summary=f"Driver reported a safety issue: {text[:200]}")
    if esc.delivery_status != "DELIVERED":
        esc = deliver(conn, esc, clock)
    _set_state(conn, thread, "NOTHING", {}, clock)
    return Reply(True, "I've alerted the coordinator now. If anyone is hurt, call 112 first.", state="NOTHING",
                 escalation_id=esc.escalation_id)


def _open_thread(conn, s):
    """The session's chat thread, unless it was closed."""
    row = s.thread_id and conn.execute(
        "SELECT thread_status FROM chat_threads WHERE thread_id = ?", (s.thread_id,)).fetchone()
    return s.thread_id if row and row["thread_status"] != "CLOSED" else None


def _new_thread(conn, s, r, clock):
    """Open a chat thread in the client's chat_threads table and tie it to the session."""
    thread = _id("DPT")
    conn.execute(
        "INSERT INTO chat_threads (thread_id, driver_id, shipment_id, opened_at, thread_status, thread_intent) "
        "VALUES (?, ?, ?, ?, 'OPEN', ?)",
        (thread, s.driver_id, s.shipment_id, to_iso(clock.now()), r.intent if r.intent in THREAD_INTENTS else "UNKNOWN"))
    conn.execute("UPDATE driver_sessions SET thread_id = ? WHERE session_id = ?", (thread, s.session_id))
    return thread


def handle_message(conn, session_id, text, clock, settings=SETTINGS, reader=read_message_ai, message_ref=None):
    """One driver turn: check login, save, read, decide what to ask or do, save the reply. A resent message_ref
    gets the same reply again."""
    s = get_session(conn, session_id, clock)
    if s is None:
        return Reply(False, "Please log in again.")
    thread = _open_thread(conn, s)
    if message_ref and thread:
        old = conn.execute("SELECT message_text FROM chat_messages WHERE thread_id = ? AND external_message_id = ?",
                           (thread, f"re:{message_ref}")).fetchone()
        if old:
            return Reply(True, old["message_text"], thread, _state(conn, thread)[0])
    r = reader(text, clock.now())
    thread = thread or _new_thread(conn, s, r, clock)
    key = f"{thread}:{_post(conn, thread, 'DRIVER', s.driver_id, text, clock, r, message_ref)}"
    awaiting, note = _state(conn, thread)
    if SAFETY.search(text.lower()):
        reply = _safety(conn, s, thread, text, clock)
    elif s.shipment_id is None:
        reply = _pick_shipment(conn, s, thread, text, r, awaiting, note, key, clock, settings, reader)
    elif awaiting == "ETA":
        reply = _answer_eta(conn, s.shipment_id, thread, text, r, note, key, clock, settings)
    elif awaiting == "CHOICE":
        reply = _answer_choice(conn, s.shipment_id, thread, text, r, note, key, clock, settings)
    elif awaiting == "WAREHOUSE":
        reply = _waiting(conn, s.shipment_id, thread, note, clock)
    else:
        reply = _fresh(conn, s.shipment_id, thread, text, r, key, clock, settings)
    return _finish(conn, thread, reply, clock, external=f"re:{message_ref}" if message_ref else None)


def notify_booking_result(conn, appointment_id, clock, settings=SETTINGS):
    """Tell the driver the warehouse's answer in their chat; after a rejection, show fresh options (R5)."""
    a = conn.execute(
        """SELECT a.*, d.dock_code, sl.slot_start_ts FROM appointments a JOIN appointment_slots sl ON sl.slot_id = a.slot_id
           JOIN docks d ON d.dock_id = sl.dock_id WHERE a.appointment_id = ?""", (appointment_id,)).fetchone()
    thread = a and _thread_for(conn, appointment_id)
    if not thread:
        return Reply(False, "No chat to tell.")
    where = f"{a['dock_code']} at {_hm(a['slot_start_ts'])}"
    if a["appointment_status"] == "CONFIRMED":
        _set_state(conn, thread, "NOTHING", {}, clock)
        ref = f", ref {a['warehouse_confirmation_ref']}" if a["warehouse_confirmation_ref"] else ""
        return _finish(conn, thread, Reply(True, f"Confirmed: {where}{ref}.", state="NOTHING",
                                           appointment_id=appointment_id), clock, status="RESOLVED")
    if a["appointment_status"] == "REJECTED":
        reply = _offer(conn, a["shipment_id"], thread, f"{thread}:{appointment_id}", clock, settings,
                       intro=f"The warehouse couldn't take {where}: {a['cancellation_reason']}. ")
        return _finish(conn, thread, replace(reply, appointment_id=appointment_id), clock)
    return Reply(False, "The warehouse hasn't answered yet.", thread)


def check_silent_drivers(conn, clock, settings=SETTINGS, out=None):
    """No reply to the time or options question: nudge once after 5 min, close the chat 5 min later.
    Closing while waiting for the arrival time also opens an ETA_UNRESOLVED escalation. Returns what it did."""
    expire_holds(conn, clock)
    now, done = clock.now(), []
    rows = conn.execute(
        """SELECT cs.*, t.shipment_id FROM conversation_state cs JOIN chat_threads t USING (thread_id)
           WHERE cs.awaiting IN ('ETA', 'CHOICE') AND t.thread_status <> 'CLOSED'""").fetchall()
    for row in rows:
        thread, note, asked = row["thread_id"], json.loads(row["note"]), datetime.fromisoformat(row["asked_at"])
        if row["awaiting"] == "CHOICE" and note.get("hold") is None:
            continue  # still in a collection window (dp-10)
        if row["nudged_at"] is None and now >= asked + timedelta(minutes=settings.nudge_after_minutes):
            if row["awaiting"] == "CHOICE":
                o = note["options"][note["held"]]
                text = (f"Are you still there? {o['dock']} {_hm(o['start'])} is held for you until "
                        f"{get_hold(conn, note['hold']).expires_at:%H:%M}. Reply {note['held'] + 1} to book it.")
            else:
                text = f"Still there? Please reply with your arrival time, like {note['example']}."
            _post(conn, thread, "AGENT", "dockpilot", text, clock, intent="NUDGE")
            conn.execute("UPDATE conversation_state SET nudged_at = ? WHERE thread_id = ?", (to_iso(now), thread))
            done.append({"thread_id": thread, "action": "NUDGED", "escalation_id": None})
        elif row["nudged_at"] and now >= asked + timedelta(
                minutes=settings.nudge_after_minutes + settings.close_after_minutes):
            esc = None
            if row["awaiting"] == "ETA":
                guess = f" Best guess {_hm(note['guess'])}." if note.get("guess") else ""
                esc = create_escalation(conn, "ETA_UNRESOLVED", row["shipment_id"], clock, thread_id=thread,
                                        summary=f"Driver went silent after being asked for an arrival time.{guess}")
                if esc.delivery_status != "DELIVERED":
                    esc = deliver(conn, esc, clock, out)
            _post(conn, thread, "AGENT", "dockpilot", CLOSE_TEXT, clock, intent="CLOSED")
            conn.execute("UPDATE chat_threads SET thread_status = 'CLOSED', closed_at = ? WHERE thread_id = ?",
                         (to_iso(now), thread))
            conn.execute("UPDATE conversation_state SET awaiting = 'NOTHING', updated_at = ? WHERE thread_id = ?",
                         (to_iso(now), thread))
            done.append({"thread_id": thread, "action": "CLOSED", "escalation_id": esc and esc.escalation_id})
    return done


def notify_allocation(conn, decisions, clock, settings=SETTINGS):
    """Tell each driver how a collection window ended (dp-10): the winner gets the held slot, a loser the reason and
    fresh options (R5). Returns the replies posted."""
    out = []
    for d in decisions:
        _, note = _state(conn, d.thread_id) if d.thread_id else (None, {})
        if note.get("request") != d.request_id:
            continue  # no chat, or the driver has moved on
        if d.status in ("WON", "NEAR_TIE"):
            extra = (" Another truck asked at almost the same time, so a coordinator may still change this."
                     if d.status == "NEAR_TIE" else "")
            reply = _held_reply(conn, d.thread_id, note["options"], note["held"], d.hold, clock, extra=extra)
        else:
            reply = _offer(conn, d.shipment_id, d.thread_id, f"{d.thread_id}:{d.request_id}", clock, settings,
                           intro=f"{d.reason} ")
        out.append(_finish(conn, d.thread_id, reply, clock))
    return out


def notify_warehouse_timeout(conn, escalations, clock):
    """The warehouse didn't answer in time (dp-08 escalated it): tell the waiting driver once (dp-19)."""
    out = []
    for esc in escalations:
        thread = esc.appointment_id and _thread_for(conn, esc.appointment_id)
        awaiting, note = _state(conn, thread) if thread else (None, {})
        if awaiting != "WAREHOUSE" or note.get("appointment") != esc.appointment_id or note.get("escalated"):
            continue
        _set_state(conn, thread, "WAREHOUSE", {**note, "escalated": esc.escalation_id}, clock)
        slot = note.get("slot", "your slot")
        out.append(_finish(conn, thread, Reply(
            True, f"The warehouse hasn't answered about {slot} yet, so I've passed your request to a coordinator with "
                  "all the details. Someone will be in touch soon. Keep heading to the warehouse; your request is "
                  "still in place.", state="WAREHOUSE", escalation_id=esc.escalation_id), clock))
    return out
