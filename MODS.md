# Twitch Channel Points Miner - personal mods

Add-ons for https://github.com/rdavydov/Twitch-Channel-Points-Miner-v2 (upstream code is unmodified).

- `miner/run.py` - my config. Lives in the repo root (force-added; upstream .gitignore ignores it). Set your username; password=None prompts at first login.
- `TaskScripts/MinerSupervisor.ps1` - supervisor: auto-update, crash rollback, Pushover alerts, login-code push.
- `TaskScripts/MinerTask.ps1` - install/start/stop/health/summary Task Scheduler tasks.
- `TaskScripts/miner_wrapper.py` - Python launcher used by the supervisor.
- `TaskScripts/miner-alerts.example.json` - rename to `miner-alerts.json` and fill in Pushover keys.

No cookies, logs, passwords or tokens are included.

