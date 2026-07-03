# Installing the local web app

This runs the Investment Engine as a local web app on `http://localhost:8000`,
optionally auto-starting at login via `launchd`. Single-user, local-only —
not exposed to the network (CORS restricts to `http://localhost:8000`, and
`launcher.sh` binds to `127.0.0.1`, not `0.0.0.0`).

## 1. Install dependencies

```bash
cd "/Users/joeelhajj/Projects/Investment Engine"
source .venv/bin/activate
pip install -r requirements.txt
```

(This pulls in `fastapi` and `uvicorn[standard]`, added alongside the
existing `requests`/`PyYAML`/`yfinance` — nothing in `engine/` changed.)

## 2. Try it manually first

```bash
uvicorn app.main:app --host 127.0.0.1 --port 8000 --reload
```

Open `http://localhost:8000` in a browser. Ctrl-C to stop. Confirm this
works before wiring up auto-start.

## 3. Auto-start at login (optional)

```bash
cp app/ie.plist ~/Library/LaunchAgents/
launchctl load ~/Library/LaunchAgents/ie.plist
```

The agent runs `app/launcher.sh`, which activates the venv and starts
`uvicorn app.main:app --host 127.0.0.1 --port 8000`. Logs go to
`~/.investment_engine/server.log` (both stdout and stderr).

## 4. Open the dashboard

Open Chrome or Safari → `http://localhost:8000`.

**Optional — install as a standalone app (no browser chrome):** in Chrome,
open the three-dot menu → "Save and share" → "Install page as app". This
adds a Dock icon that opens the dashboard in its own window — a personal
Bloomberg terminal, launched like any other Mac app.

## Stopping / updating

```bash
# stop the auto-start agent
launchctl unload ~/Library/LaunchAgents/ie.plist

# update: pull latest code, then just let it restart
git pull
# KeepAlive means launchd restarts the process automatically the next
# time it exits (e.g. after `launchctl kickstart -k`, or a crash) —
# no separate "redeploy" step is needed for a `git pull` to take effect
# on the next natural restart. To force an immediate restart:
launchctl kickstart -k gui/$(id -u)/com.joeelhajj.investmentengine
```

## Known limitations (by design, for a single-user local tool)

- The SQLite watchlist (`~/.investment_engine/watchlist.db`) and the
  in-memory screen-job / analyze caches are single-process. Don't run two
  `uvicorn` instances against the same watchlist at once.
- The EDGAR on-disk cache (`.cache/edgar/*.json`) is written with plain
  `write_text()`, not an atomic write — see `architecture_snapshot.md` §6.
  Fine for one person clicking around; not hardened for concurrent access.
- `/api/screen` and `/api/analyze/{ticker}` are genuinely slow (EDGAR +
  yfinance calls) — expect 30–120s for a full watchlist screen and
  10–30s for a single-ticker analysis the first time (same-day repeats of
  `/api/analyze` are served from an in-memory cache).
