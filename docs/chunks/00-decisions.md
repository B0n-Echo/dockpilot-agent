# Decisions: DockPilot build

Source spec: `docs/01_problem_definition_sheet.md`, `docs/02_data_audit.md`, `docs/03_decision_register.md` (D5 holds, D6 states, D7 allocation), and the challenge brief's hand-ins #3–#5.
Date: 2026-11-19

## Resolved

- **Name:** DockPilot, repo `dockpilot-agent` (public). Descriptions use no company names, so the project can be presented as a general product.
- **Stack:** Python 3.12 managed by `uv`, the standard-library `sqlite3` module, and `pytest`. The data is already SQLite; no extra database server is needed.
- **Reading driver messages:** an LLM (a small Claude model via OpenRouter) only turns free text into fields (intent, ETA, shipment reference). Every decision (feasibility, holds, bookings, escalation) is made in deterministic code, as PDS §2.5 requires. A rule-based reader implements the same interface as the fallback.
- **API key safety:** the OpenRouter key lives in a local `.env` that git ignores. Only `.env.example` (blank value) is committed. The key is never pasted into chat.
- **Tests run offline:** tests and the integrity proof always use the rule-based reader, so they are free, fast and give the same result every run. The LLM is used only in the chat simulator and the scenario run.
- **Data:** the full data package (`.db`, schema SQL, data dictionary, guide, ER image) is committed as-is to `data/source/`. It is dummy data, so publishing it is fine. The challenge brief PDF stays out of the repo.
- **Original data is never written to:** at runtime the code copies `data/source/setuhaul_freight_operations.db` to `data/work/` (git-ignored) and applies our additive migration there: `slot_holds`, `escalations`, and an event log. Resetting means copying again. Reason: tests and integrity runs write thousands of rows; the client's data must stay clean and git must not show the `.db` as changed.
- **Clock:** an injectable clock. The fake clock starts at the snapshot time (2026-08-04 09:45 +05:30) and tests can move it forward, so hold expiry and timeouts are tested instantly. Live use reads real time.
- **Interface:** a command-line chat simulator (you type as the driver) plus scripted scenarios. No web UI for now.
- **Warehouse confirmation:** simulated with a confirm / reject command, used by a person or by the scenario script.
- **Escalations:** written to an `escalations` table with the full M3 hand-off record (driver, shipment, current appointment, latest ETA + source, options checked and why each failed, driver constraints) and printed to the console.
- **Spike mode:** on when a facility receives 5 or more slot requests within 10 minutes. Configurable, pending the client's answer (U4 / Q3).
- **Integrity test:** N separate processes, each with its own connection, race for one slot; about 100 rounds; duplicates and retries mixed in; a check query after every round (M1 = 0). Optional naive (no-lock) version shows the test catches double booking.
- **Repo layout:** `src/`, `tests/`, `docs/`, `data/`. The existing docs move into `docs/`.

## Deferred

- **D1–D3 details** (fields to collect, chat-to-shipment matching rule, text-to-ETA rule): deferred because: they need working code and real messages to test against; they are settled inside the conversation chunks.
- **Exact OpenRouter model name:** deferred because: confirmed when the LLM reader is wired up; it is a setting.
- **Client answers Q1–Q13:** deferred because: they depend on the client's reply; the build uses configurable defaults until then.
