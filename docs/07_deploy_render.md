# Deploying DockPilot on Render

Step by step, from the repo to a public HTTPS address. It takes about 15 minutes the first time.

The service is described in [`render.yaml`](../render.yaml): one always-on Python web service, with fresh base data on every start, a health check at `/healthz`, and an automatic redeploy on every push to `main`. **Secret values are never in the repo:** you paste them into Render.

---

## Before you start

- The latest code is committed and **pushed** to GitHub (`origin` → `B0n-Echo/dockpilot-agent`, branch `main`).
- Your Render account can see that GitHub repo: Render → Account settings → GitHub → give access to `dockpilot-agent`.
- Have these values ready, from your `.env` and your own choice:

| Setting | Value | Notes |
|---|---|---|
| `OPENROUTER_API_KEY` | your OpenRouter key | Never paste it in chat or commit it |
| `OPENROUTER_MODEL` | e.g. `anthropic/claude-haiku-5.5` | Same as your `.env` |
| `LANGSMITH_TRACING` | `true` | `false` keeps the key but sends nothing |
| `LANGSMITH_API_KEY` | your LangSmith key | |
| `LANGSMITH_PROJECT` | e.g. `dockpilot-render` | A separate project keeps online traces apart from your laptop's |
| `WAREHOUSE_PASSWORD` | a password you choose | Protects the warehouse page, its buttons and "+10 min" |

## Steps

1. Render dashboard → **New** → **Blueprint**.
2. Pick the repo **dockpilot-agent**, branch **main**. Render reads `render.yaml` and shows one web service, **dockpilot**.
3. Render asks for the six values above (they're marked `sync: false`). Paste them, then **Apply**.
4. Wait for the build, about 1–2 minutes. In **Logs** you should see, in order:
   - `pip install uv` … `uv sync` … installed packages
   - `Working database ready…`, then `Driver chat: http://0.0.0.0:10000 … (password set)`
   - the health check passing; the service turns **Live**
5. Open the service's address, `https://dockpilot-xxxx.onrender.com`:
   - **The driver chat** opens: log in as Kailash (code on screen), as on your laptop.
   - **`/warehouse`**: the browser asks for a user name and password. Any user name, plus your `WAREHOUSE_PASSWORD`.
   - **LangSmith:** traces appear in the project you named.
6. Send me the address, and I'll run the full demo on it.

## Day to day

| You want to… | Do this |
|---|---|
| **Reset the demo** to fresh data and the clock to 09:45 | Render → dockpilot → **Manual Deploy → Restart service** (every start begins from the base data) |
| **Ship a change** | Push to `main`; Render redeploys automatically |
| **Change a secret** | Render → dockpilot → **Environment** → edit → Save (the app restarts) |
| **See errors** | Render → dockpilot → **Logs** (a crash's full error is printed there; drivers only see a friendly sentence) |

**Before a live presentation:** restart the service about 5 minutes before you start. The demo clock starts at 09:45 when the app starts, not when you open the page.

## If something goes wrong

| What you see | Likely cause | Fix |
|---|---|---|
| "No open ports detected" | The app isn't listening on `0.0.0.0:$PORT` | Check the start command matches `render.yaml` exactly |
| Build fails at `uv sync --frozen` | `uv.lock` is out of date with `pyproject.toml` | On your laptop: `uv lock`, commit, push |
| Deploy stuck, then health check failing | The app crashed on start | Read the first error in **Logs** (often a missing setting) |
| The chat works but reads like the rules (no AI) | `OPENROUTER_API_KEY` / `OPENROUTER_MODEL` missing or wrong | Fix in **Environment** |
| No traces in LangSmith | `LANGSMITH_TRACING` isn't `true`, or the key is wrong | Fix in **Environment** |
| `/warehouse` never asks for a password | `WAREHOUSE_PASSWORD` not set | Set it in **Environment** |

**Never scale to more than one instance.** The "one writer at a time" lock and the timer live inside one process, and `render.yaml` sets `numInstances: 1`.

## Moving to AWS later

Nothing in the code is Render-specific. On an AWS virtual machine (Lightsail or EC2) you'd:
1. Install `uv`.
2. Run the same start command, with `--port 80` behind HTTPS, or behind a load balancer.
3. Set the same six settings as environment variables.
4. Keep it running with a service manager (systemd).

To keep data between restarts there, drop `--reset` and set `DOCKPILOT_DB` to a path on the machine's disk.
