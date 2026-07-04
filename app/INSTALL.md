# Installing the local web app

This runs the Investment Engine as a local web app on `http://localhost:8000`,
auto-starting at login via `launchd`. Single-user, local-only — not exposed
to the network (CORS restricts to `http://localhost:8000`, and
`launcher.sh` binds to `127.0.0.1`, not `0.0.0.0`).

`launcher.sh` runs the app through your **conda base environment's**
`uvicorn`, resolved to an absolute path — not `.venv`, and not whatever
happens to be on `PATH`. This matters because `launchd` runs the script
non-interactively: it never sources `~/.zshrc` or activates conda the way
an open Terminal does, so anything relying on `PATH` or `conda activate`
silently fails under `launchd` even though it works fine when you run it
by hand.

## 1. Install dependencies in the conda base env

```bash
# from any shell — this is the one command that DOES rely on your normal
# PATH, because you're running it yourself, not launchd
conda activate base
cd "/Users/joeelhajj/Projects/Investment Engine"
pip install -r requirements.txt
```

(This pulls in `fastapi` and `uvicorn[standard]`, added alongside the
existing `requests`/`PyYAML`/`yfinance` — nothing in `engine/` changed.)

## 2. Try it manually first

```bash
cd "/Users/joeelhajj/Projects/Investment Engine"
conda activate base
uvicorn app.main:app --host 127.0.0.1 --port 8000 --reload
```

Open `http://localhost:8000` in a browser. Ctrl-C to stop. Confirm this
works before wiring up auto-start.

You can also confirm `launcher.sh` itself resolves the right `uvicorn`
before installing it as a launchd agent:

```bash
bash -x app/launcher.sh
# watch for the resolved CONDA_BASE / uvicorn path in the trace, then Ctrl-C
```

## 3. Auto-start at login

### 3a. Give the launchd job your Anthropic API key

Tier 2's flag extraction (`/api/flags/{ticker}`) needs `ANTHROPIC_API_KEY` at
runtime. The key must never be committed to this repo, and `launchd` jobs
don't inherit your shell's environment (no `~/.zshrc`, no `export`), so it
has to be injected directly into the installed plist's own
`EnvironmentVariables` — from a file that lives outside the repo entirely.

Create `~/.investment_engine/env.plist` (this directory is NOT part of the
git repo, so nothing here can ever be committed):

```bash
mkdir -p ~/.investment_engine
cat > ~/.investment_engine/env.plist <<'EOF'
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>EnvironmentVariables</key>
    <dict>
        <key>ANTHROPIC_API_KEY</key>
        <string>sk-ant-REPLACE-WITH-YOUR-REAL-KEY</string>
    </dict>
</dict>
</plist>
EOF
chmod 600 ~/.investment_engine/env.plist   # readable only by you — it holds a live API key
```

### 3b. Install the plist and merge the key in

```bash
cp app/com.joeelhajj.investmentengine.plist ~/Library/LaunchAgents/
/usr/libexec/PlistBuddy -c "Merge ~/.investment_engine/env.plist" \
  ~/Library/LaunchAgents/com.joeelhajj.investmentengine.plist
launchctl load ~/Library/LaunchAgents/com.joeelhajj.investmentengine.plist
```

`PlistBuddy Merge` adds `env.plist`'s top-level `EnvironmentVariables` dict
into the COPY sitting in `~/Library/LaunchAgents` — the tracked template in
this repo (`app/com.joeelhajj.investmentengine.plist`) is never touched and
never carries a key. `launchd` sets that environment on the job's process;
`launcher.sh`'s `exec` inherits it straight through to `uvicorn`, no code
change needed there.

If you ever rotate the key: edit `~/.investment_engine/env.plist`, re-run
the `PlistBuddy Merge` + `cp` step above (Merge overwrites the existing
`EnvironmentVariables` key rather than duplicating it), then
`launchctl kickstart -k gui/$(id -u)/com.joeelhajj.investmentengine` to
restart with the new value.

Verify it's actually listening:

```bash
lsof -i :8000
# COMMAND   PID      USER   FD   TYPE ...
# Python  12345 joeelhajj   ...  TCP  127.0.0.1:8000 (LISTEN)
```

If nothing shows up, check the log:

```bash
tail -50 ~/.investment_engine/server.log
```

## 4. Open the dashboard — and pin it to the Dock

Open Chrome → `http://localhost:8000`.

Chrome's three-dot menu (top right) → **Save and Share** → **Install page
as app**. This adds a Dock icon that opens the dashboard in its own
window, no browser chrome — a personal Bloomberg terminal, launched like
any other Mac app, that's already running by the time you click it
(`RunAtLoad` means it started at login).

## Restarting / updating

```bash
# pull latest code
cd "/Users/joeelhajj/Projects/Investment Engine"
git pull

# KeepAlive means launchd restarts the process automatically after it
# exits, but a `git pull` alone doesn't make the ALREADY-RUNNING process
# pick up new code — force an immediate restart to apply it:
launchctl kickstart -k gui/$(id -u)/com.joeelhajj.investmentengine
```

## Uninstalling

```bash
launchctl unload ~/Library/LaunchAgents/com.joeelhajj.investmentengine.plist
rm ~/Library/LaunchAgents/com.joeelhajj.investmentengine.plist
```

(This stops the agent and removes it from login items. The repo, the
SQLite watchlist at `~/.investment_engine/watchlist.db`, and the EDGAR
disk cache are untouched — only the auto-start registration is removed.)

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
