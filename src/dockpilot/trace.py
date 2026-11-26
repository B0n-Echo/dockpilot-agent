"""Trace mode (dp-20): records every step a driver message goes through, to SQLite and, with a key, to LangSmith.

    uv run python -m dockpilot.trace --last          # the last recorded turn as a tree
    uv run python -m dockpilot.trace --turn <id>     # a chosen turn

Off by default; on with DOCKPILOT_TRACE=1 (.env or environment) or the web demo's --trace.
"""

import argparse
import contextvars
import functools
import os
import re
import sqlite3
import threading
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone

from dockpilot import db, paths

_forced = None
"""True/False when set_enabled was called (wins over .env); None means: follow DOCKPILOT_TRACE."""
_turn = contextvars.ContextVar("dockpilot_turn", default=None)
_span = contextvars.ContextVar("dockpilot_span", default=None)
PHONE = re.compile(r"(\d{5})\d{3}(\d{2})\b")
"""A 10-digit phone number; the middle three digits are hidden."""


def mask(text):
    """Hide the middle of any 10-digit phone number, e.g. 9000010013 -> 90000•••13."""
    return PHONE.sub(r"\1•••\2", str(text))


def set_enabled(on=True):
    """Switch tracing on or off for this process (wins over DOCKPILOT_TRACE); None goes back to the setting."""
    global _forced
    _forced = on


def enabled():
    """Is tracing on? set_enabled wins; otherwise DOCKPILOT_TRACE=1 in the environment or .env."""
    if _forced is not None:
        return _forced
    from dockpilot.llm_reader import load_env  # here, not at the top: llm_reader imports this module
    return os.environ.get("DOCKPILOT_TRACE", load_env().get("DOCKPILOT_TRACE")) == "1"


@dataclass
class Span:
    """One recorded step: name, kind (turn/step/decision/db/ai/timer), parent, timing, short in/out summary, notes."""

    name: str
    kind: str
    parent_id: str | None
    inp: str = ""
    out: str = ""
    error: str = ""
    notes: list = field(default_factory=list)
    span_id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    started: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    ms: float = 0.0


@dataclass
class Turn:
    """One driver message or one timer tick: its spans and the chat it belongs to."""

    name: str
    label: str
    turn_id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    thread_id: str | None = None
    spans: list = field(default_factory=list)


class _Off:
    """Stand-in span when tracing is off: accepts writes, keeps nothing."""

    out = inp = error = ""
    notes = []

    def __setattr__(self, name, value):
        """Ignore every write."""


@contextmanager
def span(name, kind="step", inp=""):
    """Record one step inside the current turn (does nothing outside a turn or when tracing is off)."""
    if _turn.get() is None:
        yield _Off()
        return
    s = Span(name, kind, getattr(_span.get(), "span_id", None), mask(inp))
    token, t0 = _span.set(s), time.perf_counter()
    try:
        yield s
    except Exception as exc:
        s.error = f"{type(exc).__name__}: {exc}"[:300]
        raise
    finally:
        s.ms = round((time.perf_counter() - t0) * 1000, 1)
        s.out = mask(s.out)
        _span.reset(token)
        _turn.get().spans.append(s)


def note(text):
    """Add a remark to the current step (e.g. why the AI fell back to the rules)."""
    s = _span.get()
    if s is not None and _turn.get() is not None:
        s.notes.append(mask(text))


def tag(thread_id=None):
    """Tie the current turn to a chat thread, so the driver's web panel can find it."""
    t = _turn.get()
    if t is not None and thread_id and not t.thread_id:
        t.thread_id = thread_id


def traced(name, kind="step", show_in=None, show_out=None):
    """Decorator: record each call of the function as a step, with optional one-line in/out summaries."""
    def wrap(fn):
        @functools.wraps(fn)
        def inner(*args, **kwargs):
            if _turn.get() is None:
                return fn(*args, **kwargs)
            with span(name, kind, show_in(*args, **kwargs) if show_in else "") as s:
                result = fn(*args, **kwargs)
                s.out = show_out(result) if show_out else ""
                return result
        return inner
    return wrap


@contextmanager
def turn(conn, name, label="", quiet=False):
    """One driver message or timer tick: collect its steps, then save them (and send them to LangSmith).
    quiet=True keeps the turn only if some step reported something (for the 5-second timer)."""
    if not enabled() or _turn.get() is not None:
        yield None
        return
    t = Turn(name, mask(label)[:200])
    token = _turn.set(t)
    try:
        with span(name, "turn", label) as root:
            yield root
    finally:
        _turn.reset(token)
        if not quiet or any(s.out for s in t.spans if s.kind != "turn"):
            _save(conn, t)
            _send_to_langsmith(t)


def _save(conn, t):
    """Write a turn and its steps to the trace tables of the working database."""
    root = next(s for s in t.spans if s.kind == "turn")
    try:
        conn.execute("INSERT INTO trace_turns (turn_id, name, label, thread_id, started_at, ms) VALUES (?, ?, ?, ?, ?, ?)",
                     (t.turn_id, t.name, t.label, t.thread_id, root.started.isoformat(), root.ms))
        conn.executemany(
            "INSERT INTO trace_spans (span_id, turn_id, parent_id, name, kind, started_at, ms, summary_in, summary_out, "
            "notes, error) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [(s.span_id, t.turn_id, s.parent_id, s.name, s.kind, s.started.isoformat(), s.ms, s.inp, s.out,
              " · ".join(s.notes), s.error) for s in t.spans])
    except sqlite3.Error as exc:
        print(f"trace: not saved ({exc})")


def _langsmith_settings():
    """(api key, project) for LangSmith from the environment or .env; key None when not set."""
    from dockpilot.llm_reader import load_env
    env = {**load_env(), **{k: v for k, v in os.environ.items() if k.startswith("LANGSMITH_")}}
    return env.get("LANGSMITH_API_KEY"), env.get("LANGSMITH_PROJECT", "dockpilot")


def _post(t, key, project):
    """Send one finished turn to LangSmith as a run tree (the AI step as an LLM run). Never raises."""
    try:
        from datetime import timedelta

        from langsmith import Client
        from langsmith.run_trees import RunTree
        kinds = {"turn": "chain", "ai": "llm", "decision": "chain", "db": "tool", "step": "chain", "timer": "chain"}
        end = lambda s: s.started + timedelta(milliseconds=s.ms)
        root_span = next(s for s in t.spans if s.kind == "turn")
        root = RunTree(name=f"{t.name}: {t.label}"[:120], run_type="chain", inputs={"message": root_span.inp},
                       start_time=root_span.started, project_name=project, ls_client=Client(api_key=key),
                       extra={"metadata": {"turn_id": t.turn_id, "thread_id": t.thread_id}})
        runs = {root_span.span_id: root}
        for s in sorted(t.spans, key=lambda s: s.started):
            if s is root_span:
                continue
            parent = runs.get(s.parent_id, root)
            runs[s.span_id] = child = parent.create_child(
                s.name, kinds.get(s.kind, "chain"), inputs={"input": s.inp}, start_time=s.started)
            child.end(outputs={"output": s.out, "notes": s.notes}, error=s.error or None, end_time=end(s))
        root.end(outputs={"reply": root_span.out}, error=root_span.error or None, end_time=end(root_span))
        root.post(exclude_child_runs=False)
        root.ls_client.flush()
    except Exception as exc:  # tracing must never break the agent
        print(f"trace: LangSmith send failed ({type(exc).__name__})")


def _send_to_langsmith(t):
    """With a LANGSMITH_API_KEY, send the turn in the background so the driver's reply is not delayed."""
    key, project = _langsmith_settings()
    if key:
        threading.Thread(target=_post, args=(t, key, project), daemon=True).start()


def show(conn, turn_id):
    """A saved turn as an indented tree, with milliseconds per step."""
    turn_row = conn.execute("SELECT * FROM trace_turns WHERE turn_id = ?", (turn_id,)).fetchone()
    if turn_row is None:
        return f"No trace {turn_id}."
    rows = conn.execute("SELECT * FROM trace_spans WHERE turn_id = ? ORDER BY started_at, rowid", (turn_id,)).fetchall()
    kids = {}
    for r in rows:
        kids.setdefault(r["parent_id"], []).append(r)
    lines = [f"{turn_row['name']} · {turn_row['label']}  ({turn_row['started_at'][11:19]} UTC, {turn_row['ms']:.0f} ms)"]

    def walk(parent, pad):
        """Add the children of one step, indented."""
        items = kids.get(parent, [])
        for i, r in enumerate(items):
            last = i == len(items) - 1
            shown_in = r["summary_in"]
            if r["kind"] == "ai" and "[user]" in shown_in:  # fold the fixed instruction; stored and sent in full
                system, user = shown_in.split("[user]", 1)
                shown_in = f"[system] (fixed instruction, {system.count(chr(10)) + 1} lines) [user]{user}"
            what = " → ".join(x for x in (shown_in, r["summary_out"]) if x)
            extra = "".join(f"  [{x}]" for x in (r["notes"], r["error"] and f"error: {r['error']}") if x)
            ai = " ★AI" if r["kind"] == "ai" else ""
            lines.append(f"{pad}{'└─' if last else '├─'} {r['name']}{ai}  {r['ms']:.0f} ms  {what}{extra}")
            walk(r["span_id"], pad + ("   " if last else "│  "))

    root = next((r for r in rows if r["kind"] == "turn"), None)
    if root:
        if root["summary_out"]:
            lines.append(f"   reply: {root['summary_out']}")
        walk(root["span_id"], "")
    return "\n".join(lines)


def main(argv=None):
    """Print a saved turn from the working database."""
    p = argparse.ArgumentParser(description="Show a DockPilot trace.")
    g = p.add_mutually_exclusive_group(required=True)
    g.add_argument("--last", action="store_true")
    g.add_argument("--turn")
    p.add_argument("--db", default=str(paths.WORK_DB))
    args = p.parse_args(argv)
    conn = db.connect(args.db)
    turn_id = args.turn or (conn.execute("SELECT turn_id FROM trace_turns ORDER BY started_at DESC, rowid DESC LIMIT 1")
                            .fetchone() or [None])[0]
    print(show(conn, turn_id) if turn_id else "No traces yet. Turn tracing on and send a message.")


if __name__ == "__main__":
    main()
