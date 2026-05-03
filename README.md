<div align="center">

<img src="docs/logo.svg" alt="cc-session-browser" width="160" height="160">

# cc-session-browser

[![Python 3.11+](https://img.shields.io/badge/python-3.11%2B-3776AB?logo=python&logoColor=white)](https://www.python.org/)
[![FastAPI](https://img.shields.io/badge/FastAPI-0.110%2B-009688?logo=fastapi&logoColor=white)](https://fastapi.tiangolo.com/)
[![License: MIT](https://img.shields.io/badge/license-MIT-E8954A.svg)](LICENSE)
[![Local-first](https://img.shields.io/badge/local--first-✓-58D070)](https://www.inkandswitch.com/local-first/)
[![No build step](https://img.shields.io/badge/no%20build%20step-✓-58D070)]()
[![Mobile-friendly](https://img.shields.io/badge/mobile-friendly-58D070)]()
[![PRs welcome](https://img.shields.io/badge/PRs-welcome-E8954A.svg)](CONTRIBUTING.md)

A local web UI to search, browse, and resume **Claude Code** sessions across all
projects on a single dev machine.

</div>

Inspired by Raycast's precision retrieval and a photographer's contact sheet —
designed for *recognition under partial memory* when you've got hundreds of
past sessions and need to find the right one to resume.

![cc-session-browser screenshot](docs/screenshot.png)

> Reads `~/.claude/history.jsonl` and `~/.claude/projects/<encoded-cwd>/<sid>.jsonl`
> directly from disk. Nothing leaves your machine — purely local.

## Features

- **Search-first**: a Raycast-shaped hero search box that scopes against topic,
  project name, or full transcript text (optional `--full` flag). Debounced,
  highlight-on-match.
- **Date-grouped contact sheet**: sessions cluster under `Today`, `Yesterday`,
  `Wed Apr 30`, etc. Sticky group headers, scan many sessions fast.
- **Persistent detail pane** (desktop): click a row → right-hand pane shows the
  initial prompt + the last assistant output + a copy-pasteable resume command.
  Mobile (≤900px) collapses to a slide-in drawer.
- **Real activity sort**: orders by transcript file mtime (catches resumes,
  tool calls, autonomous work) — not just last user-message timestamp.
- **Live indicator**: green `●` for any session whose transcript was modified in
  the last 60 seconds.
- **Infinite scroll**: 50 sessions per page, IntersectionObserver loads more as
  you scroll. Handles 500+ session archives without lag.
- **URL state**: every filter + search term lands in the URL. Share a search
  result link from desktop to phone over Tailscale.
- **Filter chips**: today / this week / long (>30min) / errored (transcript has
  tool errors) / full-text search.
- **Mobile-first responsive**: 16px inputs (no iOS zoom), 44px tap targets,
  works great over Tailscale from a phone.

## Architecture

| File | Responsibility |
|---|---|
| `server.py` | FastAPI app — reads `~/.claude/`, exposes `/api/sessions`, `/api/session/<sid>` |
| `static/index.html` | 60-line shell — markup only, no logic |
| `static/app.css` | All design tokens + layout (Field.io-inspired dark theme) |
| `static/app.js` | `Api` (data) · `Store` (state) · `View` (DOM) · `App` (controller) — strict SOLID separation |

## Install

Requires Python 3.11+. Tested on Debian 13.

```bash
git clone https://github.com/Joncik91/cc-session-browser.git
cd cc-session-browser
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

## Run

```bash
# Defaults: bind 127.0.0.1:8766, CORS allows loopback only
.venv/bin/uvicorn server:app --host 127.0.0.1 --port 8766
```

Open `http://127.0.0.1:8766/` in your browser.

## Configure (env vars)

| Var | Default | Purpose |
|---|---|---|
| `ALLOWED_ORIGINS` | `http://127.0.0.1:8766,http://localhost:8766` | Comma-separated CORS allowlist. Add your LAN/Tailscale URLs to access from phone or other devices. |

Example for LAN + Tailscale access:

```bash
ALLOWED_ORIGINS="http://127.0.0.1:8766,http://192.168.1.10:8766,http://100.64.0.5:8766" \
  uvicorn server:app --host 0.0.0.0 --port 8766
```

## Run as a systemd service

See `examples/cc-session-browser.service`. Copy to `/etc/systemd/system/`,
edit paths + env vars, then:

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now cc-session-browser
```

## Security

**Do not expose this to the public internet.** Your transcripts contain every
prompt you've ever sent Claude — code, secrets, personal context. Bind to
loopback or LAN only, or hide it behind Tailscale / WireGuard.

The default config binds to `127.0.0.1` for this reason. Switching to
`--host 0.0.0.0` requires explicit intent — make sure your firewall scopes the
port to trusted networks.

## Contributing

PRs welcome. See [CONTRIBUTING.md](CONTRIBUTING.md) — the codebase is
intentionally small (no build step, single-file frontend, stdlib + FastAPI),
which means the design philosophy matters more than usual.

## License

MIT. See `LICENSE`.
