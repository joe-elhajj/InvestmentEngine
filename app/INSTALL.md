# Installing the local web app

This runs the Investment Engine as a local web app on `http://localhost:8000`,
auto-starting at login via `launchd`. Single-user, local-only — not exposed
to the network (CORS restricts to `http://localhost:8000`, and
`launcher.sh` binds to `127.0.0.1`, not `0.0.0.0`).

`launcher.sh` resolves the repository from its own location and runs
uvicorn with `.venv/bin/python`. No shell activation is needed. The
installed launchd configuration records the actual clone and log paths.

## 1. Install dependencies

Follow [README Setup](../README.md#setup) to create the pinned environment.
Run the installation commands below from the root of your clone.

## 2. Try it manually first

```bash
./app/launcher.sh
```

Open `http://localhost:8000` in a browser. Ctrl-C to stop. Confirm this
works before wiring up auto-start.

You can also confirm `launcher.sh` itself resolves the right `uvicorn`
before installing it as a launchd agent:

```bash
bash -x app/launcher.sh
# watch for the resolved repository / .venv Python path, then Ctrl-C
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
.venv/bin/python - <<'PY'
import plistlib
from pathlib import Path

root = Path.cwd().resolve()
name = "com.joeelhajj.investmentengine.plist"
with (root / "app" / name).open("rb") as source:
    job = plistlib.load(source)
job["ProgramArguments"] = ["/bin/bash", str(root / "app" / "launcher.sh")]
log_dir = Path.home() / ".investment_engine"
log_dir.mkdir(parents=True, exist_ok=True)
job["StandardOutPath"] = job["StandardErrorPath"] = str(log_dir / "server.log")
target = Path.home() / "Library" / "LaunchAgents" / name
target.parent.mkdir(parents=True, exist_ok=True)
with target.open("wb") as output:
    plistlib.dump(job, output)
PY
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

Do not load the tracked template directly: launchd requires the generated
absolute paths and does not expand shell variables. If you move the clone,
unload the installed job, recreate `.venv` using README Setup, and repeat
step 3b from the new location.

If you ever rotate the key: edit `~/.investment_engine/env.plist`, re-run
the generation and `PlistBuddy Merge` commands above (Merge overwrites the existing
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
# from the root of your clone
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
