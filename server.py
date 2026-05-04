#!/usr/bin/env python3
"""
history-browser — local FastAPI service that lets you search/filter Claude Code
sessions across all projects on this machine and copy the resume command.

Sources:
  ~/.claude/history.jsonl                       (one line per user message)
  ~/.claude/projects/<encoded-cwd>/<sid>.jsonl  (full transcript)

The full session list is rebuilt from disk on every /api/sessions request — cheap
enough at 174 sessions, and avoids cache-staleness when new prompts arrive.
Per-session transcript scans are cached in-memory keyed by file mtime.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import time
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

HOME = Path(os.environ.get("HOME", "/root"))
HISTORY = HOME / ".claude" / "history.jsonl"
PROJECTS = HOME / ".claude" / "projects"
STATIC = Path(__file__).resolve().parent / "static"

NOISE_PASTE_RE = re.compile(r"^\s*(\[Pasted text[^\]]*\]\s*)+$")
PASTE_MARKER_RE = re.compile(r"\[Pasted text[^\]]*\]")
SINCE_RE = re.compile(r"^(\d+)([hdw])$")

# transcript scan cache: {sid: (mtime, summary_dict)}
_transcript_cache: dict[str, tuple[float, dict]] = {}
# git branch cache: {proj_path: (timestamp, branch_or_none)}
_branch_cache: dict[str, tuple[float, str | None]] = {}
BRANCH_TTL = 60.0  # re-resolve branch at most once a minute


def is_noise(msg: str) -> bool:
    msg = msg.strip()
    if not msg:
        return True
    if msg.startswith("/"):
        return True
    if NOISE_PASTE_RE.match(msg):
        return True
    if len(msg) < 8:
        return True
    return False


def derive_topic(msgs: list[str]) -> str:
    cleaned: list[str] = []
    for m in msgs:
        if is_noise(m):
            continue
        cleaned.append(PASTE_MARKER_RE.sub("", m).strip())
    if not cleaned:
        return msgs[0].strip() if msgs else "(no content)"
    topic = cleaned[0]
    i = 1
    while len(topic) < 60 and i < len(cleaned) and i < 3:
        topic = topic + " / " + cleaned[i]
        i += 1
    return topic


def fmt_duration(ms: int) -> str:
    s = ms // 1000
    h, rem = divmod(s, 3600)
    m, _ = divmod(rem, 60)
    if h:
        return f"{h}h {m}m"
    if m:
        return f"{m}m"
    return "<1m"


# Project-parent dirs — paths under any of these get bucketed by their next
# segment. Defaults match the most common conventions (apps/, Projects/,
# projects/, code/, src/, repos/). Override via PROJECT_PARENTS env var
# (comma-separated, no leading/trailing slashes).
DEFAULT_PROJECT_PARENTS = "apps,Projects,projects,code,src,repos"
_parents_raw = os.environ.get("PROJECT_PARENTS", DEFAULT_PROJECT_PARENTS)
PROJECT_PARENTS = tuple(p.strip() for p in _parents_raw.split(",") if p.strip())
APPS_RE = re.compile(
    r"(?:^|/)(?:" + "|".join(re.escape(p) for p in PROJECT_PARENTS) + r")/([^/]+)(?:/|$)"
)

# Rename map — old `apps/<name>` → new name. When a transcript references a
# project that's since been renamed, every edit is credited to the new name
# instead. Env-driven so it works the same in this repo and in user
# deployments. Format: PROJECT_RENAMES='{"old":"new","old2":null}' — null
# means "truly deleted, do not credit." Loaded once at module import.
def _load_renames() -> dict[str, str | None]:
    raw = os.environ.get("PROJECT_RENAMES", "").strip()
    if not raw:
        return {}
    try:
        d = json.loads(raw)
        if not isinstance(d, dict):
            return {}
        return {str(k): (None if v is None else str(v)) for k, v in d.items()}
    except Exception:
        return {}


PROJECT_RENAMES: dict[str, str | None] = _load_renames()


def canonical_project(name: str) -> str | None:
    """Apply PROJECT_RENAMES once. Returns None if the name maps to null
    (explicit "deleted, don't credit"); the new name if remapped; otherwise
    the original name unchanged.
    """
    if name in PROJECT_RENAMES:
        return PROJECT_RENAMES[name]
    return name
# Fallback path-prefix → label mapping for non-apps work. Matched in order;
# first hit wins. Lets a hooks-only session show "project: .claude" instead of
# vanishing under the apps/ filter.
FALLBACK_BUCKETS: list[tuple[str, str]] = [
    ("/root/.claude/", ".claude"),
    ("/home/joncik/.claude/", ".claude"),
    ("/root/claude-obsidian/", "claude-obsidian"),
    ("/home/joncik/claude-obsidian/", "claude-obsidian"),
    ("/etc/", "system"),
    ("/usr/local/", "system"),
]


# Existence cache for `apps/<name>` dirs — skips re-statting on every request.
# A project deleted after the session ran shouldn't keep appearing in the UI;
# 10-min TTL is short enough to reflect deletes within a coffee break.
_project_exists_cache: dict[str, tuple[float, bool]] = {}
PROJECT_EXISTS_TTL = 600.0
# Roots to look under for project basename existence. Every (root, parent)
# combination is checked. Defaults to $HOME only, which covers the standard
# case. Override via PROJECT_ROOTS env var (comma-separated absolute paths)
# if you keep projects somewhere unusual (e.g. /opt, /workspace, or as root
# with /root in addition to $HOME).
_roots_raw = os.environ.get("PROJECT_ROOTS", "").strip()
if _roots_raw:
    PROJECT_ROOTS = [r.strip() for r in _roots_raw.split(",") if r.strip()]
else:
    PROJECT_ROOTS = [str(HOME)]
APPS_PARENTS = [
    str(Path(root, parent))
    for root in PROJECT_ROOTS
    for parent in PROJECT_PARENTS
]


def project_dir_exists(name: str) -> bool:
    now = time.time()
    cached = _project_exists_cache.get(name)
    if cached and now - cached[0] < PROJECT_EXISTS_TTL:
        return cached[1]
    exists = any(Path(parent, name).is_dir() for parent in APPS_PARENTS)
    _project_exists_cache[name] = (now, exists)
    return exists


# Labels we never gate on existence — they're stable system locations.
STABLE_LABELS = {".claude", "claude-obsidian", "system"}


def project_bucket(path: str) -> str | None:
    """Map a touched file path to a project name, or None if it's noise.
    Priority: an `apps/<name>` segment anywhere → that <name> (after applying
    PROJECT_RENAMES); otherwise the first matching FALLBACK bucket. Anything
    else (e.g. /tmp, $HOME root) is not credited to any project.
    Names mapped to null in PROJECT_RENAMES are dropped entirely.
    """
    m = APPS_RE.search(path)
    if m:
        return canonical_project(m.group(1))
    for prefix, label in FALLBACK_BUCKETS:
        if path.startswith(prefix):
            return label
    return None


def encoded_dir(proj_path: str) -> str:
    """Project paths in ~/.claude/projects/ are stored with `/` replaced by `-`.
    Example: `/home/user/apps` -> `-home-user-apps`. `/root` -> `-root`.
    """
    return proj_path.replace("/", "-")


def resume_command(proj_path: str, sid: str) -> str:
    cd = "cd ~" if proj_path in ("/root", str(HOME)) else f"cd {proj_path}"
    return f"{cd} && claude --resume {sid}"


def git_branch(proj_path: str) -> str | None:
    now = time.time()
    cached = _branch_cache.get(proj_path)
    if cached and now - cached[0] < BRANCH_TTL:
        return cached[1]
    branch: str | None = None
    if Path(proj_path).is_dir():
        try:
            r = subprocess.run(
                ["git", "-C", proj_path, "rev-parse", "--abbrev-ref", "HEAD"],
                capture_output=True, text=True, timeout=2, check=False,
            )
            if r.returncode == 0:
                branch = r.stdout.strip() or None
        except Exception:
            branch = None
    _branch_cache[proj_path] = (now, branch)
    return branch


def scan_transcript(proj_path: str, sid: str) -> dict:
    """Open transcript and extract tool counts, errored flag, token totals, files
    touched, transcript text (concatenated user+assistant), and mtime."""
    enc = encoded_dir(proj_path)
    p = PROJECTS / enc / f"{sid}.jsonl"
    if not p.exists():
        return {"missing": True}
    mtime = p.stat().st_mtime
    cached = _transcript_cache.get(sid)
    if cached and cached[0] == mtime:
        return cached[1]

    counts = {"E": 0, "B": 0, "R": 0, "G": 0, "X": 0}
    files_touched: set[str] = set()
    # Per-root edit/touch counter — top-level dir of every file written/edited.
    # Roots are 2-segment slices (e.g. /home/joncik, /etc, /root) to match how
    # humans think about "which project / area was being touched."
    root_touch_counts: dict[str, int] = defaultdict(int)
    # Project-name edit counter — the basename right after `apps/` in any file
    # path Claude wrote or edited. Sessions are usually launched from anywhere
    # but the actual code lives in `apps/<name>/...`. Falls back to other
    # buckets (`.claude`, `claude-obsidian`) when no apps/ edits exist.
    project_edit_counts: dict[str, int] = defaultdict(int)
    # Names explicitly mapped to null in PROJECT_RENAMES (truly deleted).
    # Counted separately so they never appear in the active project ranking
    # but DO show up in orphaned_projects → stale_reasons.
    null_mapped_edit_counts: dict[str, int] = defaultdict(int)
    # Distinct cwds seen across user messages, keyed by path → first-seen ms.
    # Captures mid-session `cd` jumps so the detail pane can show every
    # workspace the session moved through.
    cwd_first_seen: dict[str, int] = {}
    in_tok = out_tok = 0
    transcript_chunks: list[str] = []
    # Last assistant text block — the closing reply the user actually reads.
    # Overwritten as we walk forward; final value at EOF is the answer to
    # "where did this session leave off." Tool-only assistant turns (no text
    # block at all) don't reset this — we only update when a non-empty text
    # block is found.
    last_assistant_text = ""

    try:
        with p.open() as f:
            for line in f:
                try:
                    rec = json.loads(line)
                except Exception:
                    continue
                # tool calls live in assistant message content blocks of type tool_use
                msg = rec.get("message") or {}
                role = msg.get("role")
                content = msg.get("content")
                # cwd is a top-level field on user records (the dir Claude was
                # invoked from at the moment that prompt was sent). Track first-
                # seen timestamp per distinct cwd.
                rec_cwd = rec.get("cwd")
                rec_ts = rec.get("timestamp")
                if isinstance(rec_cwd, str) and rec_cwd and rec_cwd not in cwd_first_seen:
                    # timestamp may be ISO string or epoch ms — normalise to ms.
                    ts_ms = 0
                    if isinstance(rec_ts, (int, float)):
                        ts_ms = int(rec_ts) if rec_ts > 1e11 else int(rec_ts * 1000)
                    elif isinstance(rec_ts, str):
                        try:
                            ts_ms = int(datetime.fromisoformat(rec_ts.replace("Z", "+00:00")).timestamp() * 1000)
                        except Exception:
                            ts_ms = 0
                    cwd_first_seen[rec_cwd] = ts_ms
                # Track the last NON-EMPTY assistant text block we saw.
                if rec.get("type") == "assistant" and isinstance(content, list):
                    found_text = ""
                    for block in content:
                        if isinstance(block, dict) and block.get("type") == "text":
                            t = block.get("text") or ""
                            if isinstance(t, str) and t.strip():
                                found_text = t.strip()
                    if found_text:
                        last_assistant_text = found_text
                if isinstance(content, list):
                    for block in content:
                        if not isinstance(block, dict):
                            continue
                        btype = block.get("type")
                        if btype == "tool_use":
                            name = block.get("name", "")
                            if name in ("Edit", "Write", "NotebookEdit"):
                                counts["E"] += 1
                                inp = block.get("input") or {}
                                fp = inp.get("file_path") or inp.get("notebook_path")
                                if fp:
                                    files_touched.add(fp)
                                    # Bucket the path by its 2-segment prefix
                                    # ("/home/joncik" from "/home/joncik/apps/x.py",
                                    # "/etc" from "/etc/systemd/system/foo.service").
                                    # Top-level dirs get themselves as the root.
                                    parts = fp.split("/")
                                    if len(parts) >= 3:
                                        root = "/" + parts[1] + "/" + parts[2]
                                    elif len(parts) >= 2:
                                        root = "/" + parts[1]
                                    else:
                                        root = fp
                                    root_touch_counts[root] += 1
                                    pb = project_bucket(fp)
                                    if pb:
                                        project_edit_counts[pb] += 1
                                    else:
                                        # null-mapped (explicit deleted): keep
                                        # the original apps/<name> in a side
                                        # counter so orphan detection still
                                        # fires, but it never enters project
                                        # ranking.
                                        m = APPS_RE.search(fp)
                                        if m and m.group(1) in PROJECT_RENAMES and PROJECT_RENAMES[m.group(1)] is None:
                                            null_mapped_edit_counts[m.group(1)] += 1
                            elif name == "Bash":
                                counts["B"] += 1
                            elif name == "Read":
                                counts["R"] += 1
                            elif name == "Grep":
                                counts["G"] += 1
                        elif btype == "text":
                            t = block.get("text") or ""
                            if t:
                                transcript_chunks.append(t)
                        elif btype == "tool_result" and block.get("is_error"):
                            counts["X"] += 1
                # alt path: top-level text from user
                elif isinstance(content, str) and content:
                    transcript_chunks.append(content)
                # token usage on assistant turns
                usage = msg.get("usage") or {}
                in_tok += int(usage.get("input_tokens") or 0)
                out_tok += int(usage.get("output_tokens") or 0)
                # tool_result error variant: rec.toolUseResult.is_error
                tr = rec.get("toolUseResult")
                if isinstance(tr, dict) and tr.get("is_error"):
                    counts["X"] += 1
    except Exception:
        return {"missing": False, "error": True}

    # Cwds sorted by first-seen, drop empty paths, keep up to 8.
    cwds_sorted = sorted(
        ({"path": p, "first_seen_ms": ts} for p, ts in cwd_first_seen.items() if p),
        key=lambda x: x["first_seen_ms"],
    )[:8]
    # Path roots sorted by edit count desc, top 8 — enough for any realistic
    # session to show which areas of the filesystem got touched.
    roots_sorted = sorted(
        ({"root": r, "edits": c} for r, c in root_touch_counts.items()),
        key=lambda x: x["edits"], reverse=True,
    )[:8]
    # Project ranking — drop dirs that no longer exist on disk (project was
    # deleted/renamed after the session ran). Stable labels skip the check.
    # Sort by edit count desc, then apply dominance rule for display:
    #   - if top entry holds ≥70% of total → only that one
    #   - else show up to 3 entries above 5% threshold
    project_filtered: list[tuple[str, int]] = []
    orphaned_projects: list[dict] = []
    for label, edits in project_edit_counts.items():
        if label in STABLE_LABELS or project_dir_exists(label):
            project_filtered.append((label, edits))
        else:
            orphaned_projects.append({"name": label, "edits": edits})
    # null-mapped names always count as orphans — that's their definition.
    for label, edits in null_mapped_edit_counts.items():
        orphaned_projects.append({"name": label, "edits": edits})
    project_filtered.sort(key=lambda x: x[1], reverse=True)
    total_edits = sum(c for _, c in project_filtered)
    projects_top: list[dict] = []
    if total_edits > 0:
        threshold = max(1, int(total_edits * 0.05))
        candidates = [(n, c) for n, c in project_filtered if c >= threshold]
        if candidates and candidates[0][1] >= total_edits * 0.85:
            # Only collapse to a single name when the leader truly dominates.
            projects_top = [{"name": candidates[0][0], "edits": candidates[0][1]}]
        else:
            # Otherwise show up to 5 — sessions often span 3-5 projects.
            projects_top = [{"name": n, "edits": c} for n, c in candidates[:5]]
    summary = {
        "missing": False,
        "mtime": mtime,
        "counts": counts,
        "files_touched": sorted(files_touched)[:50],
        "files_touched_count": len(files_touched),
        "input_tokens": in_tok,
        "output_tokens": out_tok,
        "transcript_text": "\n".join(transcript_chunks)[:200_000],  # cap memory
        "last_assistant_text": last_assistant_text,
        "cwds": cwds_sorted,
        "path_roots": roots_sorted,
        "projects": projects_top,
        "orphaned_projects": sorted(orphaned_projects, key=lambda x: x["edits"], reverse=True),
    }
    _transcript_cache[sid] = (mtime, summary)
    return summary


def load_sessions() -> list[dict]:
    """Stream history.jsonl, build per-session aggregates."""
    sessions: dict[str, dict[str, Any]] = defaultdict(
        lambda: {"first_ts": None, "last_ts": None, "msgs": [], "proj_path": "", "msg_count": 0}
    )
    if not HISTORY.exists():
        return []
    with HISTORY.open() as f:
        for line in f:
            try:
                d = json.loads(line)
            except Exception:
                continue
            sid = d.get("sessionId")
            if not sid:
                continue
            ts = int(d.get("timestamp", 0))
            s = sessions[sid]
            if s["first_ts"] is None or ts < s["first_ts"]:
                s["first_ts"] = ts
            if s["last_ts"] is None or ts > s["last_ts"]:
                s["last_ts"] = ts
            s["msgs"].append(d.get("display", ""))
            s["msg_count"] += 1
            s["proj_path"] = d.get("project") or s["proj_path"]
    out: list[dict] = []
    for sid, s in sessions.items():
        proj_path = s["proj_path"] or "/"
        proj = "~" if proj_path == "/root" else (Path(proj_path).name or "/")
        topic = derive_topic(s["msgs"])
        out.append({
            "sid": sid,
            "proj": proj,
            "proj_path": proj_path,
            "first_ts": s["first_ts"],
            "last_ts": s["last_ts"],
            "duration_ms": (s["last_ts"] - s["first_ts"]) if s["first_ts"] and s["last_ts"] else 0,
            "msg_count": s["msg_count"],
            "topic": topic,
            "resume": resume_command(proj_path, sid),
        })
    out.sort(key=lambda x: x["last_ts"] or 0, reverse=True)
    return out


def parse_since(expr: str) -> datetime | None:
    """`3h`, `2d`, `1w`, or ISO `YYYY-MM-DD`."""
    if not expr:
        return None
    m = SINCE_RE.match(expr)
    if m:
        n = int(m.group(1))
        unit = m.group(2)
        delta = {"h": timedelta(hours=n), "d": timedelta(days=n), "w": timedelta(weeks=n)}[unit]
        return datetime.now(timezone.utc) - delta
    try:
        d = datetime.fromisoformat(expr)
        if d.tzinfo is None:
            d = d.replace(tzinfo=timezone.utc)
        return d
    except Exception:
        return None


def filter_sessions(
    sessions: list[dict],
    *,
    q: str = "",
    since_iso: str = "",
    project: str = "",
    cwd: str = "",
    long_only: bool = False,
    errored: bool = False,
    full_text: bool = False,
) -> list[dict]:
    out = sessions
    if since_iso:
        cutoff = parse_since(since_iso) or datetime.fromisoformat(since_iso)
        cutoff_ms = int(cutoff.timestamp() * 1000)
        out = [s for s in out if (s["last_ts"] or 0) >= cutoff_ms]
    if project:
        pl = project.lower()
        out = [s for s in out if s["proj"].lower() == pl]
    if cwd:
        out = [s for s in out if s["proj_path"] == cwd]
    if long_only:
        out = [s for s in out if s["duration_ms"] >= 30 * 60 * 1000]

    needs_transcript = errored or full_text or any(t.startswith("file:") for t in q.lower().split())
    terms = [t for t in re.split(r"\s+", q.strip()) if t]

    def matches_terms(text: str) -> bool:
        tl = text.lower()
        return all(t.lower() in tl for t in terms)

    filtered: list[dict] = []
    for s in out:
        # cheap filters first
        haystack = f"{s['topic']} {s['proj']} {s['proj_path']}"
        if terms and not full_text and not matches_terms(haystack):
            continue
        if needs_transcript:
            scan = scan_transcript(s["proj_path"], s["sid"])
            if scan.get("missing") or scan.get("error"):
                continue
            if errored and scan["counts"]["X"] == 0:
                continue
            if full_text and terms:
                if not matches_terms(haystack + "\n" + scan.get("transcript_text", "")):
                    continue
        filtered.append(s)
    return filtered


STALE_AGE_DAYS = 60
SHORT_PROMPT_THRESHOLD = 5
SHORT_OLD_DAYS = 30


def staleness_reasons(
    last_activity_ms: int, msg_count: int, projects: list[dict], orphaned: list[dict]
) -> list[str]:
    """Return zero or more reasons this session looks stale. Empty list = fresh.
    Reasons are short slugs the UI displays as a badge.

    `projects` is the surviving (existence-checked) bucket list.
    `orphaned` is the list of `apps/<name>` buckets whose dir no longer exists.
    A session whose dominant work happened in a now-deleted project is
    "deletable" even if it's recent and busy.
    """
    reasons: list[str] = []
    if last_activity_ms <= 0:
        return reasons
    age_days = (time.time() * 1000 - last_activity_ms) / 86_400_000
    if age_days >= STALE_AGE_DAYS:
        reasons.append(f"age>{STALE_AGE_DAYS}d")
    if msg_count < SHORT_PROMPT_THRESHOLD and age_days >= SHORT_OLD_DAYS:
        reasons.append(f"abandoned ({msg_count} prompts, {int(age_days)}d old)")
    # Orphan signal: a substantial (>=10 edits) bucket whose dir is gone,
    # AND no live apps/<name> sibling. If the session also touched a still-
    # living project, the orphan is just a scratch dir or sibling experiment
    # — the session itself is still meaningful and resumable.
    has_live_apps = any(p["name"] not in STABLE_LABELS for p in projects)
    significant_orphans = [o for o in orphaned if o["edits"] >= 10]
    if significant_orphans and not has_live_apps:
        names = ", ".join(o["name"] for o in significant_orphans[:3])
        reasons.append(f"deleted project: {names}")
    return reasons


def enrich(sessions: list[dict], *, with_transcript: bool = True) -> list[dict]:
    now = time.time()
    enriched: list[dict] = []
    for s in sessions:
        scan = scan_transcript(s["proj_path"], s["sid"]) if with_transcript else {"missing": True}
        live = False
        # Transcript mtime catches ANY activity (resume, assistant edits, tool
        # calls), not just user-message timestamps from history.jsonl. A session
        # reopened weeks later shows the reopen date, which is the right
        # mental model for "when did I last touch this."
        mtime = scan.get("mtime") if not scan.get("missing") else None
        if mtime:
            live = (now - mtime) < 60.0
        last_activity_ms = int(mtime * 1000) if mtime else (s["last_ts"] or 0)
        # Last assistant text — truncated for the row preview. The full text
        # is still available via /api/session/<sid> for the detail pane.
        last_text = scan.get("last_assistant_text", "") if not scan.get("missing") else ""
        last_text_preview = last_text[:300] if last_text else ""
        projects = scan.get("projects", []) if not scan.get("missing") else []
        orphaned = scan.get("orphaned_projects", []) if not scan.get("missing") else []
        stale = staleness_reasons(last_activity_ms, s["msg_count"], projects, orphaned)
        enriched.append({
            **s,
            "branch": git_branch(s["proj_path"]),
            "live": live,
            "tool_counts": scan.get("counts", {"E": 0, "B": 0, "R": 0, "G": 0, "X": 0}) if not scan.get("missing") else None,
            "files_touched": scan.get("files_touched", []) if not scan.get("missing") else [],
            "files_touched_count": scan.get("files_touched_count", 0) if not scan.get("missing") else 0,
            "input_tokens": scan.get("input_tokens", 0) if not scan.get("missing") else 0,
            "output_tokens": scan.get("output_tokens", 0) if not scan.get("missing") else 0,
            "duration": fmt_duration(s["duration_ms"]),
            "last_activity_ms": last_activity_ms,
            "last_iso": datetime.fromtimestamp(last_activity_ms / 1000).strftime("%Y-%m-%d %H:%M") if last_activity_ms else "",
            "last_text_preview": last_text_preview,
            "projects": projects,
            "stale_reasons": stale,
        })
    return enriched


# ---------- API ----------

app = FastAPI(title="cc-session-browser", docs_url=None, redoc_url=None)

# CORS origins are configurable via env. Defaults to loopback only — extend
# with the ports/IPs you actually serve from. Comma-separated.
# Example: ALLOWED_ORIGINS="http://192.168.1.10:8766,http://100.x.y.z:8766"
DEFAULT_ORIGINS = "http://127.0.0.1:8766,http://localhost:8766"
allowed_origins = [o.strip() for o in os.environ.get("ALLOWED_ORIGINS", DEFAULT_ORIGINS).split(",") if o.strip()]
app.add_middleware(
    CORSMiddleware,
    allow_origins=allowed_origins,
    allow_methods=["GET", "POST"], allow_headers=["*"],
)


@app.get("/api/sessions")
def api_sessions(
    q: str = Query("", description="free-text terms (AND across, lower-cased substring)"),
    since: str = Query("", description="`Nh`/`Nd`/`Nw` or ISO date"),
    project: str = Query("", description="project basename exact match"),
    cwd: str = Query("", description="proj_path exact match"),
    long_only: bool = Query(False, alias="long"),
    errored: bool = Query(False),
    stale: bool = Query(False, description="only sessions with at least one stale reason"),
    full: bool = Query(False, description="also search transcript bodies"),
    limit: int = Query(50, ge=1, le=2000),
    offset: int = Query(0, ge=0, description="for infinite-scroll pagination"),
):
    all_sessions = load_sessions()
    matched = filter_sessions(
        all_sessions, q=q, since_iso=since, project=project, cwd=cwd,
        long_only=long_only, errored=errored, full_text=full,
    )
    # Enrich the full matched set so we can sort by transcript mtime (real
    # activity, not just last user-prompt timestamp). Transcript scans are
    # mtime-cached, so steady-state cost is one stat() per session — fine.
    matched_enriched = enrich(matched, with_transcript=True)
    if stale:
        matched_enriched = [s for s in matched_enriched if s.get("stale_reasons")]
    matched_enriched.sort(key=lambda x: x.get("last_activity_ms") or 0, reverse=True)
    enriched = matched_enriched[offset:offset + limit]
    total_in = sum(e["input_tokens"] for e in enriched)
    total_out = sum(e["output_tokens"] for e in enriched)
    matched_count = len(matched_enriched) if stale else len(matched)
    return {
        "total_sessions": len(all_sessions),
        "matched": matched_count,
        "shown": len(enriched),
        "offset": offset,
        "limit": limit,
        "has_more": (offset + len(enriched)) < matched_count,
        "total_input_tokens": total_in,
        "total_output_tokens": total_out,
        "sessions": enriched,
    }


@app.get("/api/projects")
def api_projects():
    """List distinct project basenames + paths for filter chips."""
    sessions = load_sessions()
    bag: dict[str, dict] = {}
    for s in sessions:
        key = s["proj_path"]
        b = bag.setdefault(key, {"proj": s["proj"], "proj_path": s["proj_path"], "count": 0, "last_ts": 0})
        b["count"] += 1
        if (s["last_ts"] or 0) > b["last_ts"]:
            b["last_ts"] = s["last_ts"]
    items = sorted(bag.values(), key=lambda x: x["last_ts"], reverse=True)
    return {"projects": items}


def _extract_text(msg: dict) -> str:
    """Concatenate text blocks from a message; ignore tool_use/tool_result."""
    content = msg.get("content")
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, list):
        parts: list[str] = []
        for b in content:
            if isinstance(b, dict) and b.get("type") == "text":
                t = b.get("text") or ""
                if isinstance(t, str):
                    parts.append(t)
        return "\n".join(parts).strip()
    return ""


@app.get("/api/session/{sid}")
def api_session_detail(sid: str, limit: int = 30):
    """Return:
      - first_user_prompt: the very first non-empty user message (the "what was
        this session about" anchor — earlier than the topic-derivation logic
        and unfiltered).
      - last_assistant_text: the LAST text block of the final assistant turn
        (the "where did it leave off" anchor — same logic the output-guard hook
        uses when scanning the closing reply).
      - messages: first N message previews (mixed user+assistant), unchanged.
    """
    sessions = {s["sid"]: s for s in load_sessions()}
    s = sessions.get(sid)
    if not s:
        raise HTTPException(404, "session not found")
    enc = encoded_dir(s["proj_path"])
    p = PROJECTS / enc / f"{sid}.jsonl"

    msgs: list[dict] = []
    first_user = ""
    last_assistant = ""

    if p.exists():
        try:
            with p.open() as f:
                lines = f.readlines()
        except Exception:
            lines = []

        # Forward pass: first user prompt + bounded message preview.
        for line in lines:
            try:
                rec = json.loads(line)
            except Exception:
                continue
            msg = rec.get("message") or {}
            role = msg.get("role")
            if role not in ("user", "assistant"):
                continue
            text = _extract_text(msg)
            if not text:
                continue
            if not first_user and role == "user":
                first_user = text
            if len(msgs) < limit:
                msgs.append({"role": role, "text": text[:1500]})

        # Backward pass: find the LAST assistant text block. The very last
        # assistant record might be a tool_use call with no text — keep walking
        # back until we hit one that actually contains a text block (the closing
        # reply the user reads).
        for line in reversed(lines):
            try:
                rec = json.loads(line)
            except Exception:
                continue
            if rec.get("type") != "assistant":
                continue
            msg = rec.get("message") or {}
            content = msg.get("content")
            if not isinstance(content, list):
                continue
            found_text = ""
            for block in content:
                if isinstance(block, dict) and block.get("type") == "text":
                    t = block.get("text") or ""
                    if isinstance(t, str) and t.strip():
                        found_text = t.strip()  # overwrite — final text block in this entry
            if found_text:
                last_assistant = found_text
                break  # stop at the first assistant entry with real text content

    # Pull cwds + path_roots + projects from the cached transcript scan.
    scan = scan_transcript(s["proj_path"], sid)
    return {
        "sid": sid,
        "first_user_prompt": first_user[:2000],
        "last_assistant_text": last_assistant[:2000],
        "messages": msgs,
        "cwds": scan.get("cwds", []) if not scan.get("missing") else [],
        "path_roots": scan.get("path_roots", []) if not scan.get("missing") else [],
        "projects": scan.get("projects", []) if not scan.get("missing") else [],
    }


ARCHIVE_DIR = PROJECTS / "_archive"


@app.post("/api/session/{sid}/archive")
def api_archive_session(sid: str):
    """Archive (not delete) a session. Moves the transcript file to
    `~/.claude/projects/_archive/<sid>.jsonl`, writes a sidecar `.meta.json`
    capturing the original encoded-dir and proj_path so it can be restored,
    and strips that sid's lines from `history.jsonl` (after taking a `.bak`).
    Idempotent — calling twice is a no-op on the second call.
    """
    sessions = {s["sid"]: s for s in load_sessions()}
    s = sessions.get(sid)
    if not s:
        raise HTTPException(404, "session not found")
    proj_path = s["proj_path"]
    enc = encoded_dir(proj_path)
    src = PROJECTS / enc / f"{sid}.jsonl"

    ARCHIVE_DIR.mkdir(parents=True, exist_ok=True)
    dest = ARCHIVE_DIR / f"{sid}.jsonl"
    meta = ARCHIVE_DIR / f"{sid}.meta.json"

    # Move transcript (or, if already gone, just record the archive intent).
    if src.exists():
        try:
            src.replace(dest)
        except Exception as e:
            raise HTTPException(500, f"failed to move transcript: {e}") from e
    moved = dest.exists()

    # Sidecar meta — written/overwritten so restore knows the encoded-dir.
    try:
        meta.write_text(json.dumps({
            "sid": sid,
            "proj_path": proj_path,
            "encoded_dir": enc,
            "archived_at": datetime.now(timezone.utc).isoformat(),
            "msg_count": s["msg_count"],
        }, indent=2))
    except Exception as e:
        raise HTTPException(500, f"failed to write meta: {e}") from e

    # Strip sid lines from history.jsonl (with a .bak). Idempotent — if no
    # lines match we still write the file, but it'll be byte-identical.
    removed_lines = 0
    if HISTORY.exists():
        try:
            backup = HISTORY.with_suffix(HISTORY.suffix + ".bak")
            backup.write_bytes(HISTORY.read_bytes())
            kept: list[str] = []
            with HISTORY.open() as f:
                for line in f:
                    try:
                        d = json.loads(line)
                    except Exception:
                        kept.append(line)
                        continue
                    if d.get("sessionId") == sid:
                        removed_lines += 1
                        continue
                    kept.append(line)
            HISTORY.write_text("".join(kept))
        except Exception as e:
            raise HTTPException(500, f"failed to update history.jsonl: {e}") from e

    # Drop the cached scan so subsequent requests don't show ghost data.
    _transcript_cache.pop(sid, None)

    return {
        "ok": True,
        "sid": sid,
        "transcript_moved": moved,
        "archive_path": str(dest),
        "meta_path": str(meta),
        "history_lines_removed": removed_lines,
    }


@app.get("/healthz")
def health():
    n = sum(1 for _ in HISTORY.open()) if HISTORY.exists() else 0
    return {"ok": True, "history_lines": n, "transcripts_cached": len(_transcript_cache)}


@app.get("/")
def index():
    return FileResponse(STATIC / "index.html")


app.mount("/static", StaticFiles(directory=str(STATIC)), name="static")
