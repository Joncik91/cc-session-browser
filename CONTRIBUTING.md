# Contributing to cc-session-browser

Thanks for taking the time. The codebase is intentionally small — that's the
feature, not a bug. Contributions that match the design philosophy are very
welcome; contributions that bloat it less so.

## Design philosophy (read this first)

- **Single-machine, single-user.** Don't add auth, multi-tenancy, or sync —
  those are different products.
- **Local-first.** Reads `~/.claude/` directly. Don't add cloud calls,
  telemetry, or network dependencies.
- **No build step.** Frontend is hand-written ES2020 served as static files.
  Don't introduce npm, bundlers, transpilers, or framework dependencies.
- **Stack discipline.** Backend = stdlib + FastAPI + uvicorn. That's the whole
  list, on purpose. PRs that add a Python dependency need a strong "why
  stdlib won't do it" justification.
- **No `innerHTML` on user data.** All DOM construction goes through the
  `el()` helper. View renders, never escapes manually with template strings.
- **Mobile-first CSS.** Default styles are mobile; desktop layout kicks in
  inside `@media (min-width: 901px)` only.
- **SOLID separation in `app.js`.** `Api` only fetches, `Store` only holds
  state, `View` only touches DOM, `App` wires them. Don't cross those lines.

## What's a good first contribution

| Type | Examples |
|---|---|
| 🐛 Bug fix | Sort regression, broken keyboard handling, broken filter combo |
| ✨ Small feature | New filter chip, new sort key, new keyboard shortcut |
| 📝 Docs | README polish, example systemd unit for non-Linux platforms |
| ♿ Accessibility | Better ARIA, focus management, contrast fixes |
| 🎨 Visual polish | Tighter spacing, better empty states, dark/light theme switch |

## What to discuss in an issue first

- Anything that adds a runtime dependency
- Anything that changes the public API shape (`/api/sessions`, `/api/session/<sid>`)
- Auth, multi-user, or remote-access features
- Database / persistence layer (the design intentionally has none)
- Major redesigns of the layout or interaction model

Open an issue with the proposal before writing code — saves both of us time.

## Local development

Requires Python 3.11+.

```bash
git clone https://github.com/Joncik91/cc-session-browser.git
cd cc-session-browser
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
.venv/bin/uvicorn server:app --reload --host 127.0.0.1 --port 8766
```

Visit `http://127.0.0.1:8766/`. The `--reload` flag picks up server changes
automatically. Frontend changes are picked up on browser refresh.

The tool reads your real `~/.claude/` data, so you'll see your actual
sessions during development. No fixtures, no mock data — that's the point.

## Code style

- **Python**: type-hinted, fail-open on transcript errors (return
  `{"missing": True}` and continue, never raise into the request handler).
  No black / no auto-formatter — match the surrounding style.
- **JavaScript**: ES2020 modules, no transpilation. Use `const` by default,
  arrow functions for callbacks, named functions for top-level definitions.
  Strict mode (`"use strict";`) at the top of `app.js`.
- **CSS**: design tokens in `:root`, mobile-first, prefer flex/grid over
  absolute positioning, prefer CSS variables over inline styles.

## Pull request checklist

Before opening a PR:

- [ ] The change works locally (visited `http://127.0.0.1:8766/` and exercised
      the new behavior in a real browser)
- [ ] No new runtime dependencies (or you've opened an issue first)
- [ ] No `innerHTML` calls on user-derived data
- [ ] No hardcoded paths, IPs, hostnames, or personal info — use env vars
- [ ] CSS changes work on mobile (≤900px) and desktop (≥901px)
- [ ] Updated `README.md` if the change affects install/config/usage

## PR shape that's easy to merge

- One concern per PR. Bug fix + new feature = two PRs.
- Title: lowercase imperative — `fix sort regression on empty result`,
  `add keyboard shortcut to focus search`, etc.
- Body: explain *why*, not *what* (the diff shows what). What problem does
  this solve? Did you consider alternatives?
- Keep the diff small. Big diffs get long reviews.

## What I won't merge

- Anything that hits the network beyond serving the local UI
- Telemetry, analytics, "phone home" features
- Authentication or remote-access wrappers (use Tailscale / WireGuard /
  reverse proxy at the network layer)
- Frameworks (React, Vue, htmx, etc.) — the no-build-step constraint is load-bearing
- Bundlers, transpilers, build pipelines

## License

By contributing, you agree your contributions are licensed under MIT, the
same as the rest of the project.
