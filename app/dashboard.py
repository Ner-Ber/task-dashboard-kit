#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = []
# ///
"""Collect assistant state and render the dashboard.

  dashboard.py collect [--vault PATH] [--review] [-o data.json]
      Parse root TASKS.md, tasks/<category>/**/TASKS.md,
      project notes and inbox; fetch each project repo's activity with `gh`
      (read-only). Writes JSON with task_lists (per category / nested project).
      --review
      adds review_candidates: overdue, untouched 30+ days, duplicates, old next
      steps, quiet projects, old PRs, claude/* branches with no PR.
  dashboard.py ids [--vault PATH]
      Give open tasks without 🆔 a stable id (and ➕ today). Prints changed files;
      the caller commits.
  dashboard.py render data.json [stubs.json] [-o index.html]
      Render the HTML dashboard. stubs.json adds per-project "stand" lines, a
      headline, calendar and email items. When missing or empty, serve/Update
      auto-builds pass-through stubs from collected tasks (no LLM).
  dashboard.py serve [--vault PATH] [--cache DIR] [--port 8765]
      Serve the dashboard live on 127.0.0.1 only. Each load re-reads the vault
      (git pull at most once a minute). ↻ Update = pull + collect + minimal
      stubs + render (no Claude). ↻ Pull force-pulls then re-reads. GitHub
      activity and stubs come from data.json / stubs.json in --cache (default
      <vault>/dashboard). Per task: done, due, snooze, urgency (⇢),
      ↗ source (no per-task Claude chat; no P1–P3 priority chip). Multi-select
      sticky bar: Done, Urgency… (batch ⇢), Move… (domain relocate),
      Project… (same-domain project / new project relocate).
      review.json adds the weekly review panel.
      /notes: Notes tab (progress notes in the separate notes repo at
      $ASSISTANT_NOTES or <vault>/../notes; docs/decisions.md 2026-11-20): editor +
      preview, image paste, autosave, batched git sync.
  dashboard.py link-notes <slug> <code-dir>
      progress/ symlink in a code repo to its vault notes, hidden via .git/info/exclude.
  dashboard.py import-obsidian <obsidian-dir> <mapping.json> [--dry-run]
      One-time copy of Obsidian notes (+ images) into the vault's notes folders.
      Code TODO files (*todo*.md) in a project's linked repo/branch show on its
      card; ticking one commits the close to that branch on GitHub (undoable).
      Every change is one commit, pushed, and can be undone.
      Keep-style add rows on every task list POST /add (typed line or
      pasted markdown → split into tasks) into that list's TASKS.md; Today
      boxes stamp the box's urgency (📅) on new tasks. Today urgency boxes
      group by category then project.
"""

import argparse
import base64
import datetime as dt
import difflib
import hashlib
import html
import json
import os
import queue
import re
import shutil
import subprocess
import sys
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, quote, urlsplit

def _load_dotenv() -> None:
    """Read KEY=VALUE lines from the kit's .env (next to app/) without overriding the
    real environment. Keeps `uv run app/dashboard.py …` working without a wrapper."""
    for f in (Path(__file__).resolve().parent.parent / ".env", Path.cwd() / ".env"):
        if not f.is_file():
            continue
        for line in f.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            k, v = k.strip().removeprefix("export ").strip(), v.strip().strip('"').strip("'")
            v = os.path.expanduser(os.path.expandvars(v))
            os.environ.setdefault(k, v)
        break


_load_dotenv()
DEFAULT_VAULT = Path(os.environ.get("ASSISTANT_VAULT", Path.home() / "assistant")).expanduser()
DASHBOARD_BRAND = os.environ.get("DASHBOARD_BRAND", "Tasks")
ACTIVITY_DAYS = 14
URGENT_DAYS = 2
DESK_DAYS = 14
INACTIVE_STATUSES = {"done", "paused", "submitted"}


def _categories() -> dict:
    """CATEGORIES="work:Work,side:Side,life:Life" → {folder: label}, in display order.
    Folder = tasks/<folder>/ in the vault; label = the chip / filter / [TAG] text."""
    raw = os.environ.get("CATEGORIES") or "work:Work,side:Side,life:Life"
    out = {}
    for part in raw.split(","):
        folder, _, label = part.strip().partition(":")
        folder = re.sub(r"[^a-z0-9_-]", "", folder.strip().lower())
        if folder:
            out[folder] = (label or folder).strip()
    return out or {"work": "Work"}


# Top-level categories: folder names under tasks/ → chip / filter label (from .env).
CATEGORY_DIRS = _categories()
LEGACY_CATEGORY_DIRS: dict = {}
DOMAIN_ALIASES = {**{v: v for v in CATEGORY_DIRS.values()}, **{v.upper(): v for v in CATEGORY_DIRS.values()}}
DOMAIN_TAG = "(" + "|".join(re.escape(a) for a in sorted(DOMAIN_ALIASES, key=len, reverse=True)) + ")"
# Folder segment in tasks/<cat>/… paths.
CAT_FOLDER_RE = "(?:" + "|".join(re.escape(d) for d in CATEGORY_DIRS) + ")"
# Default visible rows before the list scrolls (Today boxes / By category / nested).
LIST_CAP = 6
DOMAIN_ORDER = tuple(CATEGORY_DIRS.values())
# Category picked by default in "Add a task" rows that let you choose one.
DEFAULT_CATEGORY = os.environ.get("DEFAULT_CATEGORY") or DOMAIN_ORDER[-1]
if DEFAULT_CATEGORY not in DOMAIN_ORDER:
    DEFAULT_CATEGORY = DOMAIN_ORDER[-1]

# Category colours: one palette slot per category, in order (light, light-bg, dark, dark-bg).
_PALETTE = [("#3f6f99", "#e8f0f7", "#8db6dc", "#23303b"), ("#5d7f43", "#edf3e7", "#a5c98a", "#28331f"),
            ("#9b7432", "#f6efe2", "#d9b06f", "#3a3122"), ("#4a6b6e", "#e6efef", "#8fb4b7", "#243334"),
            ("#7a4f9a", "#f1eaf6", "#c3a1e0", "#30263a"), ("#a04a5a", "#f8e9ec", "#e597a6", "#3a2228")]
PALETTE_LIGHT = " ".join(f"--c{i}:{c[0]}; --c{i}-bg:{c[1]};" for i, c in enumerate(_PALETTE))
PALETTE_DARK = " ".join(f"--c{i}:{c[2]}; --c{i}-bg:{c[3]};" for i, c in enumerate(_PALETTE))
CATEGORY_CSS = "\n".join(
    f".chip.{d}, .{d} {{ color:var(--c{i % len(_PALETTE)}); background:var(--c{i % len(_PALETTE)}-bg) }}\n"
    f".box.list-{d} {{ border-top-color:var(--c{i % len(_PALETTE)}) }}\n"
    f'.filt-cat[data-cat="{lbl}"] {{ --c:var(--c{i % len(_PALETTE)}); --cb:var(--c{i % len(_PALETTE)}-bg) }}'
    for i, (d, lbl) in enumerate(CATEGORY_DIRS.items()))

TASK_RE = re.compile(r"^\s*- \[ \] (.*)$")
CLOSED_RE = re.compile(r"^\s*- \[[x-]\] (.*)$")
DATE = r"(\d{4}-\d{2}-\d{2})"
ID_RE = re.compile(r"🆔\s*([0-9a-f]{6})")
# owner/name, optionally @branch (several projects can share one repo, one branch each).
REPO_RE = re.compile(r"^[\w.-]+/[\w.-]+(@[\w./-]+)?$")
STALE_DAYS = 30        # untouched this long: age chip, review candidate
PR_STALE_DAYS = 14
BLAME_IGNORE = ".git-blame-ignore-revs"  # bulk edits (e.g. adding 🆔) that don't count as touching a task


def normalize_domain(d: str | None) -> str | None:
    return DOMAIN_ALIASES.get(d) if d else None


# ---------------------------------------------------------------- collect

def task_id(raw: str) -> str:
    """The line's 🆔 if it has one; else a hash of the raw line (changes when the line does)."""
    m = ID_RE.search(raw)
    return m.group(1) if m else hashlib.sha1(raw.encode()).hexdigest()[:10]


def clean_text(raw: str) -> str:
    text = re.sub(r"→\s*`[^`]*`", "", raw)
    text = re.sub(r"(📅|⏳|✅|➕|👀|❌)\s*" + DATE, "", text)
    text = ID_RE.sub("", text)
    text = re.sub(rf"\[{DOMAIN_TAG}\]", "", text).replace("‼️", "").replace("🔽", "")
    return re.sub(r"\s+", " ", text).strip(" —-")


def parse_task(raw: str, source: str, section: str = "", project: str | None = None,
               line: int | None = None) -> dict:
    date = lambda e: (m.group(1) if (m := re.search(e + r"\s*" + DATE, raw)) else None)
    dom = re.search(rf"\[{DOMAIN_TAG}\]", raw)
    link = re.search(r"memory/projects/([\w-]+)\.md", raw)
    prio = 1 if "‼️" in raw else 3 if "🔽" in raw else 2
    return {
        "id": task_id(raw),
        "text": clean_text(raw),
        "prio": prio,
        "domain": normalize_domain(dom.group(1) if dom else None),
        "due": date("📅"),
        "scheduled": date("⏳"),
        "added": date("➕"),
        "reviewed": date("👀"),
        "touched": None,  # set by collect from git blame
        "project": link.group(1) if link else project,
        "source": source,
        "section": section,
        "line": line,
    }


def parse_tasks_md(path: Path, source: str | None = None, project: str | None = None,
                   domain: str | None = None) -> list[dict]:
    """Parse open tasks from a TASKS.md. `source` is the vault-relative path shown in the UI."""
    if not path.is_file():
        return []
    source = source or "TASKS.md"
    domain = normalize_domain(domain)
    tasks, section = [], ""
    for n, line in enumerate(path.read_text().splitlines(), 1):
        if line.startswith("## "):
            section = line[3:].strip()
        elif (m := TASK_RE.match(line)) and section != "Done":
            t = parse_task(m.group(1), source, section, project=project, line=n)
            if domain and not t["domain"]:
                t["domain"] = domain
            tasks.append(t)
    return tasks


def discover_task_files(vault: Path) -> list[dict]:
    """Category + nested project TASKS.md under tasks/<category>/ (CATEGORIES in .env)."""
    out = []
    root = vault / "tasks"
    if not root.is_dir():
        return out
    cats = dict(CATEGORY_DIRS)
    for legacy, domain in LEGACY_CATEGORY_DIRS.items():
        if (root / legacy).is_dir() and legacy not in cats:
            cats[legacy] = domain
    for cat, domain in cats.items():
        cat_dir = root / cat
        if not cat_dir.is_dir():
            continue
        rollup = cat_dir / "TASKS.md"
        if rollup.is_file():
            out.append({"path": rollup, "source": f"tasks/{cat}/TASKS.md",
                        "domain": domain, "project": None, "kind": "category",
                        "title": domain, "key": cat})
        for proj in sorted(p for p in cat_dir.iterdir()
                           if p.is_dir() and not p.name.startswith(".") and p.name != "__pycache__"):
            pf = proj / "TASKS.md"
            if pf.is_file():
                out.append({"path": pf, "source": f"tasks/{cat}/{proj.name}/TASKS.md",
                            "domain": domain, "project": proj.name, "kind": "project",
                            "title": proj.name.replace("-", " "), "key": f"{cat}/{proj.name}"})
    return out


def task_list_files(vault: Path) -> list[Path]:
    """Every markdown file that can hold open tasks (root, tasks/ tree, projects, inbox)."""
    files = []
    if (vault / "TASKS.md").is_file():
        files.append(vault / "TASKS.md")
    files.extend(tf["path"] for tf in discover_task_files(vault))
    proj = vault / "memory/projects"
    if proj.is_dir():
        files.extend(p for p in sorted(proj.glob("*.md")) if not p.name.startswith("_"))
    inbox = vault / "inbox"
    if inbox.is_dir():
        files.extend(sorted(inbox.glob("*.md")))
    return files


def closed_tasks(text: str, source: str) -> list[dict]:
    return [{"text": clean_text(m.group(1)), "source": source}
            for line in text.splitlines() if (m := CLOSED_RE.match(line))]


def section_text(text: str, name: str) -> str:
    m = re.search(rf"^## {name}\s*\n(.*?)(?=^## |\Z)", text, re.M | re.S)
    return re.sub(r"\s+", " ", m.group(1)).strip() if m else ""


def next_steps(text: str) -> list[dict]:
    """Prose bullets under `## Next steps …`, each ending with its note's date `(YYYY-MM-DD)`."""
    m = re.search(r"^## Next steps[^\n]*\n(.*?)(?=^## |\Z)", text, re.M | re.S)
    out = []
    for b in re.findall(r"^- (.+(?:\n  .+)*)", m.group(1) if m else "", re.M):
        d = re.search(r"\(" + DATE + r"\)\s*$", b)
        out.append({"text": re.sub(r"\s+", " ", b).strip(), "date": d.group(1) if d else None})
    return out


def ignored_revs(vault: Path) -> set[str]:
    f = vault / BLAME_IGNORE
    return set(re.findall(r"^[0-9a-f]{40}", f.read_text(), re.M)) if f.exists() else set()


def git_date(vault: Path, rel: str) -> str | None:
    """Date of the last commit that changed rel, skipping BLAME_IGNORE revs."""
    r = subprocess.run(["git", "-C", str(vault), "log", "-20", "--format=%H %cs", "--", rel],
                       capture_output=True, text=True)
    ignored = ignored_revs(vault)
    return next((d for sha, d in (l.split() for l in r.stdout.splitlines()) if sha not in ignored), None)


def blame_dates(vault: Path, rel: str) -> dict[int, str]:
    """Line number -> date of the last commit that changed it, skipping BLAME_IGNORE revs.
    A line still blamed on an ignored rev (it can't be traced past it) is left out."""
    ignore_file = vault / BLAME_IGNORE
    ignored = ignored_revs(vault)
    cmd = ["git", "-C", str(vault), "blame", "--line-porcelain"]
    if ignored:
        cmd += ["--ignore-revs-file", str(ignore_file)]
    r = subprocess.run(cmd + ["--", rel], capture_output=True, text=True)
    out, sha, line = {}, None, 0
    for l in r.stdout.splitlines():
        if re.match(r"^[0-9a-f]{40} \d+ \d+", l):
            sha, line = l.split()[0], int(l.split()[2])
        elif l.startswith("committer-time ") and sha not in ignored:
            out[line] = (dt.date.today().isoformat() if set(sha) == {"0"}
                         else dt.date.fromtimestamp(int(l.split()[1])).isoformat())
    return out


def set_touched(vault: Path, tasks: list[dict]) -> None:
    """touched = latest of: last edit of the line (git blame), ➕ added, 👀 reviewed."""
    blames: dict[str, dict] = {}
    for t in tasks:
        if t["line"] is None:
            continue
        b = blames.setdefault(t["source"], blame_dates(vault, t["source"]))
        t["touched"] = max(filter(None, [b.get(t["line"]), t["added"], t["reviewed"]]), default=None)


def parse_project(vault: Path, path: Path) -> dict:
    text = path.read_text()
    slug = path.stem
    title_line = next((l for l in text.splitlines() if l.startswith("# ")), f"# {slug}")
    tag = re.search(rf"\[{DOMAIN_TAG}[^\]]*\]", title_line)
    title = re.sub(rf"\s*\[{DOMAIN_TAG}\]\s*$", "", title_line[2:]).strip()
    dom = re.search(rf"^\*\*Domain:\*\*\s*{DOMAIN_TAG}", text, re.M)
    status = re.search(r"^\*\*Status:\*\*\s*([a-z]+)", text, re.M)
    # Repo line: GitHub owner/name tokens (local paths ignored). Related line: shown as-is.
    repos = [tok for line in re.findall(r"^\*\*Repo:\*\*(.*)$", text, re.M)
             for tok in re.findall(r"`([^`]+)`", line) if REPO_RE.match(tok)]
    related = [tok for line in re.findall(r"^\*\*Related repos:\*\*(.*)$", text, re.M)
               for tok in re.findall(r"`([^`]+)`", line)]
    # Local line: where the code lives on disk (any machine); shown as-is, never read.
    local = [l.strip() for l in re.findall(r"^\*\*Local:\*\*(.*)$", text, re.M)]
    src = f"memory/projects/{path.name}"
    subtasks = [parse_task(m.group(1), src, project=slug, line=n)
                for n, line in enumerate(text.splitlines(), 1) if (m := TASK_RE.match(line))]
    return {
        "slug": slug,
        "title": title,
        "domain": normalize_domain(dom.group(1) if dom else (tag.group(1) if tag else None)),
        "status": status.group(1) if status else None,
        "repos": repos,
        "related": related,
        "local": local,
        "next_action": section_text(text, "Next action")[:400],
        "subtasks": subtasks,
        "closed": closed_tasks(text, src),
        "next_steps": next_steps(text),
        "updated": git_date(vault, src),
    }


def gh_json(path: str):
    r = subprocess.run(["gh", "api", "-H", "Accept: application/vnd.github+json", path],
                       capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError(r.stderr.strip()[:200])
    return json.loads(r.stdout)


def split_repo(spec: str) -> tuple[str, str | None]:
    """'owner/name@branch' -> ('owner/name', 'branch'); no @ -> branch None."""
    repo, _, branch = spec.partition("@")
    return repo, branch or None


def repo_activity(spec: str, since: str) -> dict:
    """GitHub activity for 'owner/name' or 'owner/name@branch'. With a branch, commits and
    PRs are only that branch's, and claude/* branches are left to the repo-wide entry."""
    repo, branch = split_repo(spec)
    sha = f"&sha={quote(branch, safe='')}" if branch else ""
    try:
        meta = gh_json(f"repos/{repo}")
        commits = gh_json(f"repos/{repo}/commits?since={since}T00:00:00Z&per_page=10{sha}")
        last = commits[:1] or gh_json(f"repos/{repo}/commits?per_page=1{sha}")
        issues = [] if branch else gh_json(f"repos/{repo}/issues?state=open&per_page=30")
        pulls = gh_json(f"repos/{repo}/pulls?state=open&per_page=30")
        closed = gh_json(f"repos/{repo}/pulls?state=closed&per_page=100")
        branches = [] if branch else gh_json(f"repos/{repo}/branches?per_page=100")
    except RuntimeError as e:
        return {"repo": spec, "error": str(e)}
    ref = branch or meta.get("default_branch") or "main"
    try:
        todos = code_todos(repo, ref, meta["html_url"])
    except (RuntimeError, KeyError, ValueError) as e:
        todos = [{"path": "?", "error": str(e)[:200], "items": []}]
    pr_heads = {p["head"]["ref"] for p in pulls + closed}
    if branch:
        on = lambda p: branch in (p["head"]["ref"], p["base"]["ref"])
        pulls, closed = [p for p in pulls if on(p)], [p for p in closed if on(p)]
    return {
        "repo": spec,
        "name": repo,
        "branch": branch,
        "url": meta["html_url"],
        "branch_url": f"{meta['html_url']}/tree/{branch}" if branch else meta["html_url"],
        "pushed_at": (meta.get("pushed_at") or "")[:10],
        "last_commit": last[0]["commit"]["committer"]["date"][:10] if last else "",
        "ref": ref,
        "todos": todos,
        "commits": [{"date": c["commit"]["committer"]["date"][:10],
                     "author": (c.get("author") or {}).get("login") or c["commit"]["author"]["name"],
                     "message": c["commit"]["message"].splitlines()[0][:120],
                     "url": c["html_url"]} for c in commits],
        "issues": [{"number": i["number"], "title": i["title"], "url": i["html_url"],
                    "labels": [l["name"] for l in i["labels"]], "updated": i["updated_at"][:10]}
                   for i in issues if "pull_request" not in i],
        "pulls": [{"number": p["number"], "title": p["title"], "url": p["html_url"],
                   "draft": p["draft"], "branch": p["head"]["ref"], "created": p["created_at"][:10]}
                  for p in pulls],
        "claude_branches": [b["name"] for b in branches
                            if b["name"].startswith("claude/") and b["name"] not in pr_heads],
        "merged": [{"number": p["number"], "title": p["title"], "url": p["html_url"],
                    "merged": p["merged_at"][:10], "branch": p["head"]["ref"]}
                   for p in closed if p.get("merged_at") and p["merged_at"][:10] >= since],
    }


# ---------------------------------------------------------------- code TODO files
# A linked repo/branch may keep its own backlog in markdown (e.g. docs/CODE_TODO.md).
# Two shapes are understood: "### `id`" item blocks with a "- **Status:** open" field
# (the etas CODE_TODO format), and plain "- [ ] text" checkbox lines. The dashboard
# shows the open items and can close one; closing commits to that branch on GitHub.
CODE_TODO_FILE_RE = re.compile(r"(?i)(?:^|/)[^/]*todo[^/]*\.md$")
CODE_TODO_OPEN = ("open", "in_progress", "blocked")
ITEM_HEAD_RE = re.compile(r"^###\s+(.*?)\s*$")
FIELD_RE = re.compile(r"^\s*- \*\*([A-Za-z ]+):\*\*\s*(.*)$")


def gh_path(path: str) -> str:
    return quote(path, safe="/")


def gh_file(repo: str, path: str, ref: str) -> tuple[str, str]:
    """(text, blob sha) of one file on a branch."""
    j = gh_json(f"repos/{repo}/contents/{gh_path(path)}?ref={quote(ref, safe='')}")
    return base64.b64decode(j["content"]).decode("utf-8"), j["sha"]


def find_code_todo_files(repo: str, ref: str) -> list[str]:
    tree = gh_json(f"repos/{repo}/git/trees/{quote(ref, safe='')}?recursive=1")
    return sorted(t["path"] for t in tree.get("tree", []) if t["type"] == "blob"
                  and CODE_TODO_FILE_RE.search(t["path"])
                  and not any(part.startswith(".") or part == "node_modules"
                              for part in t["path"].split("/")[:-1]))


def item_key(head: str) -> str:
    m = re.search(r"`([^`]+)`", head)
    return m.group(1) if m else head.strip()


def check_key(text: str) -> str:
    return "cb-" + hashlib.sha1(text.strip().encode()).hexdigest()[:10]


def item_blocks(lines: list[str]) -> list[tuple[int, int, str]]:
    """(start, end, key) of every "### " item; end is exclusive (next heading of level <= 3)."""
    heads = [i for i, l in enumerate(lines) if ITEM_HEAD_RE.match(l)]
    out = []
    for i in heads:
        j = next((k for k in range(i + 1, len(lines)) if re.match(r"^#{1,3}\s", lines[k])), len(lines))
        out.append((i, j, item_key(ITEM_HEAD_RE.match(lines[i]).group(1))))
    return out


def parse_code_todo(text: str) -> list[dict]:
    """Open items of a code TODO file, in file order."""
    lines = text.split("\n")
    items = []
    for start, end, key in item_blocks(lines):
        fields = {}
        for l in lines[start + 1:end]:
            if (m := FIELD_RE.match(l)):
                fields.setdefault(m.group(1).strip().lower(), m.group(2).strip())
        status = (fields.get("status") or "open").split()[0].strip("`*").lower()
        if status not in CODE_TODO_OPEN:
            continue
        goal = re.sub(r"\s+", " ", fields.get("goal", "")).strip()
        items.append({"key": key, "kind": "item", "status": status, "line": start + 1,
                      "title": goal or key, "updated": fields.get("updated", "")[:10]})
    if not items:   # checkbox-style file
        for n, l in enumerate(lines, 1):
            if (m := TASK_RE.match(l)):
                items.append({"key": check_key(m.group(1)), "kind": "check", "status": "open",
                              "line": n, "title": m.group(1).strip(), "updated": ""})
    return items


def code_todos(repo: str, ref: str, html_url: str) -> list[dict]:
    out = []
    for path in find_code_todo_files(repo, ref):
        text, _ = gh_file(repo, path, ref)
        items = parse_code_todo(text)
        if items or "todo" in path.lower():
            out.append({"path": path, "url": f"{html_url}/blob/{ref}/{path}", "items": items})
    return out


def close_code_todo(text: str, key: str, when: str) -> tuple[str, str]:
    """Mark one item done. Item blocks: Status done, Updated bumped, a Closed line, moved
    under "## Done" (created at the end if missing). Checkbox lines: ticked in place.
    Returns (new text, item title). LookupError when the item is gone or already closed."""
    lines = text.split("\n")
    if key.startswith("cb-"):
        for i, l in enumerate(lines):
            if (m := TASK_RE.match(l)) and check_key(m.group(1)) == key:
                lines[i] = l.replace("- [ ]", "- [x]", 1).rstrip() + f" ✅ {when[:10]}"
                return "\n".join(lines), m.group(1).strip()
        raise LookupError("item not found (file changed since the page loaded); press ↻ Update")
    block = next(((s, e) for s, e, k in item_blocks(lines) if k == key), None)
    if not block:
        raise LookupError("item not found (file changed since the page loaded); press ↻ Update")
    s, e = block
    while e > s + 1 and lines[e - 1].strip() in ("", "---"):   # trailing rule/blank lines stay put
        e -= 1
    body = lines[s:e]
    status_at = next((i for i, l in enumerate(body) if (m := FIELD_RE.match(l)) and m.group(1).lower() == "status"), None)
    if status_at is not None:
        cur = FIELD_RE.match(body[status_at]).group(2).split()[0].strip("`*").lower() if FIELD_RE.match(body[status_at]).group(2) else "open"
        if cur not in CODE_TODO_OPEN:
            raise LookupError(f"already {cur}")
        body[status_at] = re.sub(r"(\*\*Status:\*\*\s*).*", r"\1done", body[status_at])
    upd_at = next((i for i, l in enumerate(body) if (m := FIELD_RE.match(l)) and m.group(1).lower() == "updated"), None)
    if upd_at is not None:
        body[upd_at] = re.sub(r"(\*\*Updated:\*\*\s*).*", lambda m: m.group(1) + when, body[upd_at])
    at = (upd_at if upd_at is not None else status_at if status_at is not None else 0) + 1
    body.insert(at, f"- **Closed:** {when[:10]} — marked done from the dashboard")
    rest = lines[:s] + lines[e:]
    done_at = next((i for i, l in enumerate(rest) if re.match(r"^##\s+Done\b", l)), None)
    if done_at is None:
        while rest and not rest[-1].strip():
            rest.pop()
        rest += ["", "## Done", "", *body, ""]
    else:
        rest[done_at + 1:done_at + 1] = ["", *body]
    out = "\n".join(rest)
    out = re.sub(r"\n{3,}", "\n\n", out)
    title = item_key(ITEM_HEAD_RE.match(lines[s]).group(1))
    return out, title


_code_undo: dict[str, dict] = {}   # commit sha -> what to restore (this server's commits only)


def gh_put_file(repo: str, path: str, ref: str, text: str, sha: str, message: str) -> dict:
    body = json.dumps({"message": message, "branch": ref, "sha": sha,
                       "content": base64.b64encode(text.encode()).decode()})
    r = subprocess.run(["gh", "api", "-X", "PUT", f"repos/{repo}/contents/{gh_path(path)}", "--input", "-"],
                       input=body, capture_output=True, text=True)
    if r.returncode != 0:
        msg = r.stderr.strip() or r.stdout.strip()
        if "409" in msg or "does not match" in msg:
            raise LookupError("file changed on GitHub since it was read; press ↻ Update and retry")
        raise RuntimeError(msg[:200])
    return json.loads(r.stdout)


def code_todo_done(cache: Path, spec: str, path: str, key: str) -> dict:
    repo, branch = split_repo(spec)
    ref = branch or gh_json(f"repos/{repo}").get("default_branch") or "main"
    text, sha = gh_file(repo, path, ref)
    new, title = close_code_todo(text, key, dt.datetime.now().strftime("%Y-%m-%d %H:%M"))
    res = gh_put_file(repo, path, ref, new, sha, f"{path}: close {title[:60]} (from dashboard)")
    commit = res["commit"]["sha"]
    _code_undo[commit] = {"spec": spec, "repo": repo, "path": path, "ref": ref, "text": text,
                          "sha": res["content"]["sha"], "key": key}
    drop_cached_todo(cache, spec, path, key)
    return {"label": f"Closed in {repo.split('/')[-1]}: {title[:50]}", "commit": commit,
            "url": res["commit"].get("html_url", ""), "warn": ""}


def code_todo_undo(cache: Path, commit: str) -> dict:
    u = _code_undo.get(commit)
    if not u:
        raise LookupError("only this session's code TODO closes can be undone")
    try:
        _, cur = gh_file(u["repo"], u["path"], u["ref"])
    except RuntimeError as e:
        raise RuntimeError(f"can't read {u['path']}: {e}")
    if cur != u["sha"]:
        raise LookupError("file changed on GitHub since; undo it there")
    gh_put_file(u["repo"], u["path"], u["ref"], u["text"], cur, f"{u['path']}: reopen (undo from dashboard)")
    del _code_undo[commit]
    refresh_cached_todos(cache, u["spec"])
    return {"ok": True}


def _edit_cached_activity(cache: Path, spec: str, fn) -> None:
    f = cache / "data.json"
    if not f.exists():
        return
    with _cache_lock:
        data = json.loads(f.read_text())
        for p in data.get("projects", []):
            for a in p.get("activity", []):
                if a.get("repo") == spec:
                    fn(a)
        f.write_text(json.dumps(data, ensure_ascii=False, indent=1))


def drop_cached_todo(cache: Path, spec: str, path: str, key: str) -> None:
    def fn(a):
        for t in a.get("todos", []):
            if t["path"] == path:
                t["items"] = [i for i in t["items"] if i["key"] != key]
    _edit_cached_activity(cache, spec, fn)


def refresh_cached_todos(cache: Path, spec: str) -> None:
    repo, branch = split_repo(spec)
    meta = gh_json(f"repos/{repo}")
    todos = code_todos(repo, branch or meta.get("default_branch") or "main", meta["html_url"])
    _edit_cached_activity(cache, spec, lambda a: a.__setitem__("todos", todos))


_cache_lock = threading.Lock()


def vault_url(vault: Path) -> str:
    """https://github.com/<owner>/<repo>/blob/<branch> for the vault's origin."""
    url = git(vault, "remote", "get-url", "origin").stdout.strip()
    m = re.search(r"github\.com[:/](.+?)(?:\.git)?$", url)
    branch = git(vault, "rev-parse", "--abbrev-ref", "HEAD").stdout.strip() or "master"
    return f"https://github.com/{m.group(1)}/blob/{branch}" if m else ""


def collect(vault: Path, activity: dict | None = None, review: bool = False) -> dict:
    """activity: repo -> cached repo_activity(); None fetches it from GitHub.
    review: also compute review_candidates (slower: duplicate search).

    Task sources (prefer the tasks/ tree when present):
      TASKS.md                     — Today / pointer / leftover uncategorized
      tasks/<cat>/TASKS.md         — category rollup (one per category)
      tasks/<cat>/<project>/TASKS.md — per-project lists
      memory/projects/*.md         — project notes; checkboxes used only when no
                                     tasks/<cat>/<slug>/TASKS.md exists yet
      inbox/*.md                   — captures
    """
    today = dt.date.today()
    since = (today - dt.timedelta(days=ACTIVITY_DAYS)).isoformat()
    task_files = discover_task_files(vault)
    root_tasks = parse_tasks_md(vault / "TASKS.md", source="TASKS.md")
    cat_tasks, proj_tasks = [], []
    task_lists = []
    for tf in task_files:
        parsed = parse_tasks_md(tf["path"], source=tf["source"], project=tf["project"],
                                domain=tf["domain"])
        task_lists.append({
            "key": tf["key"], "domain": tf["domain"], "title": tf["title"],
            "project": tf["project"], "source": tf["source"], "kind": tf["kind"],
            "tasks": parsed,
        })
        (proj_tasks if tf["project"] else cat_tasks).extend(parsed)
    tasks = root_tasks + cat_tasks + proj_tasks
    proj_dir = vault / "memory/projects"
    projects = [parse_project(vault, p) for p in sorted(proj_dir.glob("*.md"))
                if proj_dir.is_dir() and not p.name.startswith("_")] if proj_dir.is_dir() else []
    tree_project_slugs = {tf["project"] for tf in task_files if tf["project"]}
    known = {p["slug"] for p in projects}
    for p in projects:
        if p["slug"] in tree_project_slugs:
            # Prefer the dedicated project TASKS.md; drop memory checkboxes to avoid dupes.
            p["subtasks"] = [t for t in proj_tasks if t["project"] == p["slug"]]
    # Card for every tasks/<cat>/<slug>/ even when memory/projects/<slug>.md is missing.
    for tf in task_files:
        if not tf["project"] or tf["project"] in known:
            continue
        known.add(tf["project"])
        projects.append({
            "slug": tf["project"],
            "title": tf["title"].replace("-", " ").title() if tf["title"] else tf["project"],
            "domain": tf["domain"],
            "status": "active",
            "repos": [],
            "related": [],
            "next_action": "",
            "subtasks": [t for t in proj_tasks if t["project"] == tf["project"]],
            "closed": [],
            "next_steps": [],
            "updated": git_date(vault, tf["source"]),
        })
    inbox = []
    inbox_dir = vault / "inbox"
    if inbox_dir.is_dir():
        for p in sorted(inbox_dir.glob("*.md")):
            for n, line in enumerate(p.read_text().splitlines(), 1):
                if m := TASK_RE.match(line):
                    inbox.append(parse_task(m.group(1), f"inbox/{p.name}", "Inbox", line=n))
    set_touched(vault, tasks + inbox + [t for p in projects for t in p["subtasks"]
                                        if t["source"].startswith("memory/projects/")])

    all_repos = sorted({r for p in projects for r in p["repos"]})
    if activity is None:
        with ThreadPoolExecutor(max_workers=8) as ex:
            activity = dict(zip(all_repos, ex.map(lambda r: repo_activity(r, since), all_repos)))
    else:
        activity = {r: activity.get(r) or {"repo": r, "error": "not in cache; run /dashboard"}
                    for r in all_repos}

    linked = {t["project"] for t in tasks if t["project"] and t["section"] in ("Active", "Waiting On")}
    linked |= tree_project_slugs
    for p in projects:
        p["activity"] = [activity[r] for r in p["repos"]]
        recent_repo = any(a.get("commits") for a in p["activity"])
        p["active"] = p["status"] not in INACTIVE_STATUSES and (
            p["status"] == "active" or p["slug"] in linked or recent_repo) or p["slug"] in linked
        p["open_tasks"] = [t for t in root_tasks + cat_tasks if t["project"] == p["slug"]]
    data = {"generated_at": dt.datetime.now().isoformat(timespec="minutes"),
            "today": today.isoformat(), "vault_url": vault_url(vault),
            "tasks": tasks, "inbox": inbox, "projects": projects, "task_lists": task_lists}
    if review:
        closed = []
        for f in [vault / "TASKS.md", *(tf["path"] for tf in task_files)]:
            if f.is_file():
                closed.extend(closed_tasks(f.read_text(), str(f.relative_to(vault))))
        closed.extend(c for p in projects for c in p["closed"])
        data["closed"] = closed
        data["review_candidates"] = candidates(data, today)
    return data


def similar(a: str, b: str) -> bool:
    s = difflib.SequenceMatcher(None, a.lower(), b.lower())
    return s.real_quick_ratio() >= 0.8 and s.quick_ratio() >= 0.8 and s.ratio() >= 0.8


def candidates(data: dict, today: dt.date) -> list[dict]:
    """Items the weekly review should ask about, with the reasons. Claude turns them into
    questions (review.json); nothing here changes the vault."""
    iso = today.isoformat()
    stale = (today - dt.timedelta(days=STALE_DAYS)).isoformat()
    live = [p for p in data["projects"] if p["status"] not in INACTIVE_STATUSES]
    opens = [t for t in data["tasks"] if t["section"] != "Someday"] + data["inbox"] + \
        [t for p in live for t in p["subtasks"]]
    out = []
    for t in opens:
        why = []
        if t["due"] and t["due"] < iso:
            why.append(f"due {t['due']} passed")
        if t["touched"] and t["touched"] < stale and not (t["scheduled"] and t["scheduled"] > iso):
            why.append(f"untouched since {t['touched']}")
        dups = [f"{o['source']}: {o['text'][:80]}" for o in opens if o is not t and o["id"] != t["id"]
                and similar(o["text"], t["text"])]
        dups += [f"closed in {c['source']}: {c['text'][:80]}" for c in data.get("closed", [])
                 if similar(c["text"], t["text"])]
        if dups:
            why.append("possible duplicate of " + "; ".join(dups[:2]))
        if why:
            out.append({"kind": "task", "task": t["id"], "src": t["source"], "text": t["text"],
                        "project": t["project"], "added": t["added"], "why": why})
    for p in live:
        src = f"memory/projects/{p['slug']}.md"
        for s in p["next_steps"]:
            if s["date"] and s["date"] < stale:
                out.append({"kind": "next_step", "src": src, "project": p["slug"], "text": s["text"],
                            "why": [f"next step from {s['date']}"]})
        recent = any(a.get("commits") for a in p.get("activity", []))
        if p["active"] and p["updated"] and p["updated"] < stale and not recent:
            out.append({"kind": "project", "src": src, "project": p["slug"], "text": p["next_action"][:200],
                        "why": [f"project notes untouched since {p['updated']}, no repo commits in {ACTIVITY_DAYS}d"]})
        for a in p.get("activity", []):
            pr_cut = (today - dt.timedelta(days=PR_STALE_DAYS)).isoformat()
            for x in a.get("pulls", []):
                if x.get("created", iso) < pr_cut:
                    out.append({"kind": "pr", "src": src, "project": p["slug"], "url": x["url"],
                                "text": f"{a['repo']} PR #{x['number']} {x['title']}",
                                "why": [f"open since {x['created']}"]})
            for b in a.get("claude_branches", []):
                out.append({"kind": "branch", "src": src, "project": p["slug"], "url": f"{a['url']}/tree/{b}",
                            "text": f"{a['repo']} branch {b}", "why": ["claude/* branch with no PR"]})
    return out


def add_ids(vault: Path) -> list[str]:
    """Give every open task without 🆔 a fresh one, and ➕ today if it has no ➕. Returns changed files."""
    files = task_list_files(vault)
    used = {m for f in files if f.is_file() for m in ID_RE.findall(f.read_text())}
    changed = []
    for f in files:
        if not f.is_file():
            continue
        lines = f.read_text().split("\n")
        new = list(lines)
        for i, l in enumerate(lines):
            if TASK_RE.match(l) and not ID_RE.search(l):
                tid = uuid.uuid4().hex[:6]
                while tid in used:
                    tid = uuid.uuid4().hex[:6]
                used.add(tid)
                l = l.rstrip()
                if "➕" not in l:
                    l += f" ➕ {dt.date.today().isoformat()}"
                new[i] = f"{l} 🆔 {tid}"
        if new != lines:
            f.write_text("\n".join(new))
            changed.append(str(f.relative_to(vault)))
    return changed


# ---------------------------------------------------------------- render

URGENCY_BUCKETS = ("anytime", "desk", "urgent")
URGENCY_LABELS = {"anytime": "Any time", "desk": "On the desk", "urgent": "Urgent"}


def bucket(t: dict, today: dt.date) -> str:
    due = dt.date.fromisoformat(t["due"]) if t["due"] else None
    sched = dt.date.fromisoformat(t["scheduled"]) if t["scheduled"] else None
    if due and due <= today + dt.timedelta(days=URGENT_DAYS):
        return "urgent"
    if sched and sched > today:
        return "snoozed"
    if (due and due <= today + dt.timedelta(days=DESK_DAYS)) or sched:
        return "desk"
    return "anytime"


def urgency_due_date(bucket_name: str, today: dt.date | None = None) -> str:
    """📅 value that places an Active task in a Today urgency bucket (clear snooze separately)."""
    today = today or dt.date.today()
    if bucket_name == "urgent":
        return today.isoformat()
    if bucket_name == "desk":
        return (today + dt.timedelta(days=max(3, DESK_DAYS // 2))).isoformat()
    if bucket_name == "anytime":
        return ""
    raise ValueError(f"bad urgency {bucket_name!r}")


def apply_urgency(line: str, bucket_name: str, today: dt.date | None = None) -> str:
    """Set 📅 for urgency bucket and clear ⏳ so the task leaves Snoozed."""
    due = urgency_due_date(bucket_name, today)
    line = set_date(line, "📅", due)
    return set_date(line, "⏳", "")


def retag_for_dest(line: str, dest: str, tag_dom: str | None) -> str:
    """Rewrite [DOMAIN] for a relocate destination; strip project links on project files."""
    m = TASK_RE.match(line)
    if not m:
        return line
    prefix, raw = m.group(0)[:m.start(1)], m.group(1)
    raw = re.sub(rf"^\s*\[{DOMAIN_TAG}\]\s*", "", raw)
    raw = re.sub(r"\s*→\s*`memory/projects/[\w-]+\.md`", "", raw)
    if re.match(rf"tasks/{CAT_FOLDER_RE}/[\w-]+/TASKS\.md$", dest):
        body = raw
    elif tag_dom:
        body = f"[{tag_dom}] {raw}"
    else:
        body = raw
    return f"{prefix}{body}".rstrip()


def copyable(code: str) -> bool:
    """Commands and longer snippets get a copy button; short identifiers don't."""
    return " " in code.strip() or len(code) > 24


def md(s: str) -> str:
    s = html.escape(s)
    s = re.sub(r"``\s?(.+?)\s?``|`([^`]+)`", lambda m: (c := m.group(1) or m.group(2)) and
               f'<code>{c}</code>' + ('<button class="cp" title="Copy">⧉</button>' if copyable(c) else ""), s)
    s = re.sub(r"\*\*([^*]+)\*\*", r"<b>\1</b>", s)
    s = re.sub(r"(https?://[^\s<]+)", r'<a href="\1">\1</a>', s)
    return s


def date_badge(t: dict, today: dt.date) -> str:
    out = []
    if is_new(t, today):
        out.append(f'<span class="badge new" title="added {t["added"]}">new</span>')
    if t["due"]:
        d = dt.date.fromisoformat(t["due"])
        delta = (d - today).days
        cls = "over" if delta < 0 else "soon" if delta <= URGENT_DAYS else ""
        label = f"{-delta}d overdue" if delta < 0 else "today" if delta == 0 else f"due {d:%b %d}"
        out.append(f'<span class="badge {cls}">{label}</span>')
    if t["scheduled"]:
        out.append(f'<span class="badge sched">from {dt.date.fromisoformat(t["scheduled"]):%b %d}</span>')
    if t.get("touched"):
        age = (today - dt.date.fromisoformat(t["touched"])).days
        if age >= STALE_DAYS:
            added = f', added {t["added"]}' if t.get("added") else ""
            out.append(f'<span class="badge old" title="untouched since {t["touched"]}{added}">{age}d</span>')
    return "".join(out)


def task_li(t: dict, today: dt.date, titles: dict, live: bool = False, show_proj: bool = True,
            vurl: str = "") -> str:
    """show_proj=False (inside a project card): no project link, no domain chip."""
    dom = f'<span class="chip {t["domain"].lower()}">{t["domain"]}</span>' if t["domain"] and show_proj else ""
    proj = f'<a class="proj" href="#p-{t["project"]}">{html.escape(titles.get(t["project"], t["project"]))}</a>' \
        if t["project"] and show_proj else ""
    live = live and t.get("id")
    box = '<input type="checkbox" class="done" title="Mark done">' if live else ""
    src = (f'<a class="src" href="{vurl}/{t["source"]}#L{t["line"]}" title="{html.escape(t["source"])}">↗</a>'
           if vurl and not live and t.get("line") else "")
    bkt = bucket(t, today) if t.get("id") else ""
    attrs = (f' data-id="{t["id"]}" data-src="{html.escape(t["source"])}"'
             f' data-due="{t["due"] or ""}" data-sched="{t["scheduled"] or ""}" data-section="{t["section"]}"'
             f' data-bucket="{bkt}"'
             + (f' data-project="{html.escape(t["project"])}"' if t.get("project") else "")
             if t.get("id") else "")
    return (f'<li data-domain="{t["domain"] or "NONE"}"{attrs}>{box}{dom}<span class="t" dir="auto">'
            f'{md(t["text"])}</span>{date_badge(t, today)}{proj}{src}{ACTS if live else ""}</li>')


ACTS = ('<span class="acts"><button data-a="src" title="Where this came from">↗</button>'
        '<button data-a="due" title="Due date">📅</button>'
        '<button data-a="snooze" title="Snooze">⏳</button>'
        '<button data-a="urgency" title="Urgency">⇢</button></span>')
REVIEW_TASK_ANSWERS = (("done", "Done"), ("keep", "Still open"), ("drop", "Drop"), ("snooze", "Snooze…"))

# Paste / typed blobs → task texts. Checklist, bullets, numbered lists; skip empties & headings.
_LIST_ITEM_RE = re.compile(
    r"^\s*(?:"
    r"[-*+]\s+\[[ xX-]\]\s+"   # - [ ] / * [x]
    r"|[-*+]\s+"                 # bullets
    r"|\d+[.)]\s+"               # 1. / 1)
    r")"
)
_HEADING_OR_RULE_RE = re.compile(r"^(?:#{1,6}\s+\S.*|-{3,}|\*{3,}|_{3,})$")


def split_task_lines(blob: str) -> list[str]:
    """Split a typed or pasted blob into separate task texts (Keep-style).

    Newlines start new tasks. Strips checklist (`- [ ]`), bullet (`-`/`*`/`+`),
    and numbered (`1.`/`1)`) markers. Skips empty lines, markdown headings
    (`# …`), and thematic breaks (`---`). Plain single line → one task.
    Whitespace-collapsed; each text capped at 400 characters.
    """
    out: list[str] = []
    for raw in (blob or "").splitlines():
        line = raw.strip()
        if not line or _HEADING_OR_RULE_RE.match(line):
            continue
        if _LIST_ITEM_RE.match(line):
            line = _LIST_ITEM_RE.sub("", line).strip()
        line = re.sub(r"^\[([ xX-])\]\s+", "", line).strip()
        line = re.sub(r"\s+", " ", line).strip()
        if not line:
            continue
        if len(line) > 400:
            line = line[:400].rstrip()
        out.append(line)
    return out


def add_row_html(*, live: bool, src: str = "", domain: str = "", project: str = "",
                 section: str = "Active", pick_domain: bool = False,
                 urgency: str = "") -> str:
    """Keep-style 'Add a task' line for live lists. pick_domain for mixed buckets.

    `urgency` (anytime/desk/urgent) stamps new tasks into that Today box via 📅.
    """
    if not live:
        return ""
    sec = html.escape(section or "Active")
    attrs = [f'data-section="{sec}"']
    if src:
        attrs.append(f'data-src="{html.escape(src)}"')
    if domain:
        attrs.append(f'data-domain="{html.escape(domain)}"')
    if project:
        attrs.append(f'data-project="{html.escape(project)}"')
    if urgency in URGENCY_BUCKETS:
        attrs.append(f'data-urgency="{urgency}"')
    if pick_domain:
        attrs.append('data-pick-domain="1"')
    sel = ""
    if pick_domain:
        sel = ('<select class="add-dom" title="Category" aria-label="Category">'
               + "".join(f'<option value="{html.escape(d)}"{" selected" if d == DEFAULT_CATEGORY else ""}>'
                         f'{html.escape(d)}</option>' for d in DOMAIN_ORDER)
               + '</select>')
    joined = " ".join(attrs)
    return (f'<li class="add-row" {joined}>'
            f'<span class="add-check" aria-hidden="true"></span>{sel}'
            f'<input type="text" class="add-inp" placeholder="Add a task…" '
            f'autocomplete="off" enterkeyhint="done" aria-label="Add a task"></li>')


def render_review(review: dict, by_id: dict, titles: dict, live: bool) -> str:
    """The weekly review queue (review.json, written by Claude). Answered items and items whose
    task is gone (done elsewhere) are hidden."""
    items = [x for x in review.get("items", []) if not x.get("answered")
             and (not x.get("task") or x["task"] in by_id)]
    lis = []
    for x in items:
        tid, src = x.get("task") or "", x.get("src", "")
        t = by_id.get(tid, {})
        src, dom = t.get("source", src), t.get("domain") or x.get("domain")
        proj = (f'<a class="proj" href="#p-{x["project"]}">{html.escape(titles.get(x["project"], x["project"]))}</a>'
                if x.get("project") else "")
        why = f'<span class="why" dir="auto">{md(x["why"])}</span>' if x.get("why") else ""
        if tid:
            btns = list(REVIEW_TASK_ANSWERS)
        else:
            btns = [("answer", a) for a in x.get("answers") or ["Done", "Still open", "Drop"]]
        bb = "".join(f'<button data-r="{a}">{html.escape(l)}</button>' for a, l in btns) + \
            '<button data-r="skip">Skip</button>'
        attrs = f' data-id="{tid}"' if tid else ""
        lis.append(f'<li class="rvi" data-rid="{html.escape(x["id"])}" data-src="{html.escape(src)}"{attrs}'
                   f' data-section="{t.get("section", "")}" data-domain="{dom or "NONE"}">'
                   f'<span class="t" dir="auto">{md(x["q"])}</span>{proj}{why}'
                   f'{"<span class=rv>" + bb + "</span>" if live else ""}</li>')
    n = len(items)
    gen = f' <span class="meta">prepared {review["generated"]}</span>' if review.get("generated") else ""
    return (f'<section class="box review" data-key="review"><h2>Review <span class="n">{n}</span>{gen}</h2>'
            f'<ul>{"".join(lis) or "<li class=empty>All answered.</li>"}</ul></section>')


GCAL_COLORS = {"1": "#7986cb", "2": "#33b679", "3": "#8e24aa", "4": "#e67c73", "5": "#f6bf26",
               "6": "#f4511e", "7": "#039be5", "8": "#616161", "9": "#3f51b5", "10": "#0b8043",
               "11": "#d50000"}
DEFAULT_EVENT_COLOR = "#4a7fd4"
HOUR_PX = 44


def minutes(hhmm: str) -> int:
    h, m = hhmm.split(":")
    return int(h) * 60 + int(m)


def render_calendar(events: list[dict], today: dt.date) -> str:
    """Day columns with time-positioned blocks, like a calendar day view."""
    days = sorted({e["date"] for e in events})[:4]
    timed = [e for e in events if not e.get("all_day") and e.get("start") and e.get("end")]
    h0 = min([8] + [minutes(e["start"]) // 60 for e in timed])
    h1 = max([18] + [-(-minutes(e["end"]) // 60) for e in timed])
    h0, h1 = max(h0, 6), min(h1, 24)
    height = (h1 - h0) * HOUR_PX

    def block_style(e, lane, lanes):
        top = (minutes(e["start"]) - h0 * 60) * HOUR_PX / 60
        hgt = max(18, (minutes(e["end"]) - minutes(e["start"])) * HOUR_PX / 60 - 2)
        color = GCAL_COLORS.get(str(e.get("color", "")), e.get("color") or DEFAULT_EVENT_COLOR)
        w = 100 / lanes
        return (f"top:{top:.0f}px;height:{hgt:.0f}px;left:calc({lane * w:.2f}% + 1px);"
                f"width:calc({w:.2f}% - 3px);background:{color}")

    heads, alldays, cols = [], [], []
    for d in days:
        date = dt.date.fromisoformat(d)
        label = "Today" if date == today else "Tomorrow" if date == today + dt.timedelta(1) else f"{date:%a}"
        heads.append(f'<div class="cal-head">{label}<span>{date:%b %d}</span></div>')
        ad = [e for e in events if e["date"] == d and (e.get("all_day") or not e.get("start"))]
        alldays.append('<div class="cal-allday">' + "".join(
            f'<div class="ev ad" dir="auto" data-domain="{e.get("domain") or "NONE"}" style="background:'
            f'{GCAL_COLORS.get(str(e.get("color", "")), e.get("color") or DEFAULT_EVENT_COLOR)}">{md(e["title"])}</div>'
            for e in ad) + "</div>")
        day_ev = sorted((e for e in timed if e["date"] == d), key=lambda e: (e["start"], e["end"]))
        # Overlap lanes: events in one overlapping cluster share its width.
        blocks, cluster, cluster_end, lane_ends = [], [], 0, []
        def flush():
            for e, lane in cluster:
                blocks.append((e, lane, len(lane_ends)))
        for e in day_ev:
            st, en = minutes(e["start"]), minutes(e["end"])
            if cluster and st >= cluster_end:
                flush(); cluster, lane_ends = [], []
            lane = next((i for i, end in enumerate(lane_ends) if end <= st), None)
            if lane is None:
                lane_ends.append(en); lane = len(lane_ends) - 1
            else:
                lane_ends[lane] = en
            cluster.append((e, lane)); cluster_end = max(cluster_end if cluster[:-1] else 0, en)
        flush()
        evs = "".join(
            f'<div class="ev" dir="auto" data-domain="{e.get("domain") or "NONE"}" style="{block_style(e, lane, lanes)}"'
            f' title="{html.escape(e["start"] + "–" + e["end"] + " " + e["title"])}">'
            f'<b>{md(e["title"])}</b><span dir="ltr">{e["start"]}–{e["end"]}</span></div>'
            for e, lane, lanes in blocks)
        cols.append(f'<div class="cal-col" data-date="{d}" style="height:{height}px">{evs}</div>')
    hours = "".join(f'<div class="cal-hour" style="top:{(h - h0) * HOUR_PX}px">{h:02d}</div>' for h in range(h0, h1))
    n = len(days)
    return (f'<section class="box info cal-box"><h2>Calendar</h2>'
            f'<div class="cal" style="--days:{n};--hour:{HOUR_PX}px" data-h0="{h0}">'
            f'<div></div>{"".join(heads)}<div></div>{"".join(alldays)}'
            f'<div class="cal-hours" style="height:{height}px">{hours}</div>{"".join(cols)}</div></section>')


NEW_DAYS = 2   # tasks added this recently sort first in every list, newest on top


def is_new(t: dict, today: dt.date | None = None) -> bool:
    added = t.get("added") or ""
    today = today or dt.date.today()
    return bool(added) and added >= (today - dt.timedelta(days=NEW_DAYS - 1)).isoformat()


def sort_key(t):
    """Stable within-group order: new tasks first (newest on top), then due/snooze date,
    then domain, then id (no P1–P3)."""
    new = is_new(t)
    return (0 if new else 1, -dt.date.fromisoformat(t["added"]).toordinal() if new else 0,
            t["due"] or t["scheduled"] or "9999", t["domain"] or "", t.get("id") or "")


def domain_rank(dom: str | None) -> int:
    """Order of CATEGORIES in .env; unknown domains last."""
    if dom in DOMAIN_ORDER:
        return DOMAIN_ORDER.index(dom)
    return len(DOMAIN_ORDER)


def today_sort_key(t: dict, titles: dict | None = None):
    """Today urgency boxes: category → project → due/snooze/id."""
    titles = titles or {}
    proj = t.get("project") or ""
    # Category rollup (no project) before named projects within a domain.
    proj_ord = (0, "") if not proj else (1, (titles.get(proj) or proj).lower())
    return (domain_rank(t.get("domain")), proj_ord[0], proj_ord[1], proj, sort_key(t))


def render_today_task_lis(items: list[dict], today: dt.date, titles: dict,
                          live: bool, vurl: str = "") -> str:
    """Task LIs for Urgent / Desk / Any time with category then project headers."""
    if not items:
        return ""
    ordered = sorted(items, key=lambda t: today_sort_key(t, titles))
    parts: list[str] = []
    prev_dom: object | None = object()
    prev_proj: object | None = object()
    for t in ordered:
        dom = t.get("domain") or "Other"
        proj = t.get("project") or ""
        if dom != prev_dom:
            parts.append(
                f'<li class="ghead gdom" data-domain="{html.escape(dom)}" aria-hidden="true">'
                f'{html.escape(dom)}</li>')
            prev_dom = dom
            prev_proj = object()
        if proj != prev_proj:
            if proj:
                label = titles.get(proj) or proj.replace("-", " ")
                p_attr = f' data-project="{html.escape(proj)}"'
            else:
                label = f"{dom} (general)"
                p_attr = ""
            parts.append(
                f'<li class="ghead gproj" data-domain="{html.escape(dom)}"{p_attr} '
                f'aria-hidden="true">{html.escape(label)}</li>')
            prev_proj = proj
        parts.append(task_li(t, today, titles, live, vurl=vurl))
    return "".join(parts)


def category_folder(domain: str) -> str | None:
    """Canonical tasks/<folder> for a UI domain label."""
    dom = normalize_domain(domain)
    return next((k for k, v in CATEGORY_DIRS.items() if v == dom), None)


def project_slug(name: str) -> str:
    """Folder slug from a project name (letters, digits, _-)."""
    raw = (name or "").strip().lower()
    raw = re.sub(r"[^\w\s-]", "", raw, flags=re.UNICODE)
    slug = re.sub(r"[\s_]+", "-", raw).strip("-")
    if not slug or not re.fullmatch(r"[\w-]+", slug):
        raise ValueError(f"bad project name {name!r}")
    return slug


def ensure_project_tasks_md(vault: Path, domain: str, slug: str, title: str = "") -> str:
    """Create tasks/<cat>/<slug>/TASKS.md if missing. Returns vault-relative path.

    Caller must hold `_lock` (and typically commit via relocate_many).
    """
    dom = normalize_domain(domain)
    cat = category_folder(dom or "")
    if not cat:
        raise ValueError(f"bad domain {domain!r}")
    if not re.fullmatch(r"[\w-]+", slug):
        raise ValueError(f"bad project slug {slug!r}")
    # Disallow clobbering the category rollup path.
    rel = f"tasks/{cat}/{slug}/TASKS.md"
    path = vault / rel
    if not path.is_file():
        heading = (title or slug.replace("-", " ")).strip() or slug
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            f"# {heading}\n\n## Active\n\n## Waiting On\n\n## Someday\n\n## Done\n")
    return rel


def minimal_stubs(data: dict, existing: dict | None = None) -> dict:
    """Pass-through stubs from collected vault state — no agent judgment.

    Headline and project stands come from task/project checkboxes and next
    action text already in the vault. Calendar/email keys are kept from
    `existing` when present (connectors are optional and Cursor-side).
    """
    existing = existing or {}
    projects: dict[str, dict] = {}
    for p in data.get("projects") or []:
        if not p.get("active"):
            continue
        prev = ((existing.get("projects") or {}).get(p["slug"]) or {})
        stand = (prev.get("stand") or "").strip()
        if not stand:
            stand = (p.get("next_action") or "").strip()
        if not stand:
            open_t = [t for t in (p.get("subtasks") or []) + (p.get("open_tasks") or [])
                      if t.get("section") != "Someday"]
            stand = open_t[0]["text"] if open_t else "No next action recorded."
        projects[p["slug"]] = {"stand": stand}
    n_open = sum(1 for t in data.get("tasks") or [] if t.get("section") == "Active")
    n_proj = sum(1 for p in data.get("projects") or [] if p.get("active"))
    headline = (existing.get("headline") or "").strip()
    if not headline:
        if n_open or n_proj:
            headline = (f"{n_open} open task{'s' if n_open != 1 else ''} across "
                        f"{n_proj} active project{'s' if n_proj != 1 else ''}.")
        else:
            headline = "No open tasks collected yet."
    return {
        "headline": headline,
        "calendar": list(existing.get("calendar") or []),
        "emails": list(existing.get("emails") or []),
        "projects": projects,
    }


def load_or_build_stubs(cache: Path, data: dict, *, rewrite: bool = False) -> dict:
    """Load stubs.json; auto-generate when missing/empty. rewrite=True refreshes stands."""
    path = cache / "stubs.json"
    existing: dict = {}
    if path.exists():
        try:
            existing = json.loads(path.read_text()) or {}
        except json.JSONDecodeError:
            existing = {}
    empty = not existing or (
        not existing.get("projects") and not (existing.get("headline") or "").strip())
    stubs = minimal_stubs(data, existing if not empty else {})
    if rewrite or empty:
        cache.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(stubs, ensure_ascii=False, indent=1))
    return stubs


def render_task_lists(task_lists: list[dict], today: dt.date, titles: dict, live: bool,
                      vurl: str = "") -> str:
    """Separate lists per category, with nested project lists under their domain."""
    if not task_lists:
        return ""
    by_dom: dict[str, list] = {d: [] for d in DOMAIN_ORDER}
    for lst in task_lists:
        by_dom.setdefault(lst["domain"], []).append(lst)
    sections = []
    for dom in DOMAIN_ORDER:
        lists = by_dom.get(dom) or []
        if not lists:
            continue
        cat = next((x for x in lists if x["kind"] == "category"), None)
        projs = [x for x in lists if x["kind"] == "project"]
        open_cat = list(cat["tasks"] if cat else [])
        lis = "".join(task_li(t, today, titles, live, vurl=vurl)
                      for t in sorted(open_cat, key=sort_key))
        cat_add = add_row_html(live=live, src=(cat or {}).get("source", ""), domain=dom)
        body = f'<ul>{lis}{cat_add or ("<li class=empty>Nothing</li>" if not lis else "")}</ul>'
        nested = []
        for pl in sorted(projs, key=lambda x: x["title"].lower()):
            items = list(pl["tasks"])
            if not items and not live:
                continue
            plis = "".join(task_li(t, today, titles, live, show_proj=False, vurl=vurl)
                           for t in sorted(items, key=sort_key))
            title = html.escape(titles.get(pl["project"], pl["title"]))
            padd = add_row_html(live=live, src=pl["source"], domain=dom, project=pl["project"] or "")
            nested.append(
                f'<details class="plist" data-key="list-{pl["key"]}" data-domain="{dom}" '
                f'data-project="{html.escape(pl["project"] or "")}" open>'
                f'<summary><h3>{title} <span class="n">{len(items)}</span></h3></summary>'
                f'<ul class="tl">{plis}{padd}</ul></details>')
        nest_html = f'<div class="nested">{"".join(nested)}</div>' if nested else ""
        n = len(open_cat) + sum(len(pl["tasks"]) for pl in projs)
        sections.append(
            f'<section class="box info counted list-{dom.lower()}" data-key="list-{dom.lower()}" '
            f'data-domain="{dom}"><h2>{dom} <span class="n">{n}</span></h2>{body}{nest_html}</section>')
    if not sections:
        return ""
    return (f'<div class="lists"><h2 class="lists-h">By category</h2>'
            f'<div class="boxes" id="lists">{"".join(sections)}</div></div>')


def render_filter_panel(task_lists: list[dict], projects: list[dict], titles: dict) -> str:
    """Table-like domain filter: parent category + nested project checkboxes."""
    by_dom: dict[str, list[tuple[str, str]]] = {d: [] for d in DOMAIN_ORDER}
    seen: dict[str, set[str]] = {d: set() for d in DOMAIN_ORDER}

    def add(dom: str | None, slug: str | None, title: str | None) -> None:
        if not dom or dom not in by_dom or not slug or slug in seen[dom]:
            return
        seen[dom].add(slug)
        by_dom[dom].append((slug, title or slug.replace("-", " ")))

    for lst in task_lists or []:
        if lst.get("kind") == "project":
            add(lst.get("domain"), lst.get("project"),
                titles.get(lst.get("project") or "", lst.get("title") or ""))
    for p in projects or []:
        add(p.get("domain"), p.get("slug"), p.get("title"))
    cols = []
    for dom in DOMAIN_ORDER:
        kids = "".join(
            f'<label class="filt-proj"><input type="checkbox" data-role="proj" '
            f'data-domain="{html.escape(dom)}" value="{html.escape(slug)}" checked>'
            f'{html.escape(title)}</label>'
            for slug, title in sorted(by_dom[dom], key=lambda x: x[1].lower()))
        cols.append(
            f'<div class="filt-cat" data-cat="{html.escape(dom)}">'
            f'<label class="filt-dom"><input type="checkbox" data-role="dom" '
            f'value="{html.escape(dom)}" checked><span>{html.escape(dom)}</span></label>'
            f'<div class="filt-kids">{kids}</div></div>')
    return (
        '<div class="filt" id="filt">'
        '<div class="filt-head">'
        '<button type="button" class="present-btn" id="presentBtn" aria-pressed="false" '
        'title="Focus for screenshare">Present</button>'
        '<span class="filt-label">Filter</span>'
        '<span class="filt-hint">double-click a name: show only it · again: show all</span></div>'
        f'<div class="filt-table">{"".join(cols)}</div></div>')


def render(data: dict, stubs: dict, live: bool = False, review: dict | None = None) -> str:
    today = dt.date.fromisoformat(data["today"])
    projects = [p for p in data["projects"] if p["active"]]
    titles = {p["slug"]: p["title"] for p in data["projects"]}
    pstubs = stubs.get("projects", {})
    vurl = data.get("vault_url", "")
    by_id = {t["id"]: t for t in data["tasks"] + data["inbox"] + [t for p in data["projects"] for t in p["subtasks"]]}

    # Buckets: Active (+ any leftover park-section) tasks from root + tasks/ tree.
    # Also dated/snoozed memory/projects checkboxes that are not mirrored in tasks/.
    pool = [t for t in data["tasks"] if t["section"] in ("Active", "Waiting On", "Someday")] \
        + list(data["inbox"])
    in_tasks = {t["id"] for t in data["tasks"]}
    pool += [t for p in projects for t in p["subtasks"]
             if t["id"] not in in_tasks and t["section"] != "Done"
             and (t["due"] or t["scheduled"])]
    # Deduplicate by id (project tasks also appear in data["tasks"]).
    seen, uniq = set(), []
    for t in pool:
        if t["id"] in seen:
            continue
        seen.add(t["id"])
        uniq.append(t)
    pool = uniq
    domains = {p["slug"]: p["domain"] for p in data["projects"]}
    for t in pool:
        t["domain"] = normalize_domain(t["domain"] or domains.get(t["project"]))
    boxes = {"urgent": [], "desk": [], "anytime": [], "snoozed": []}
    for t in pool:
        boxes[bucket(t, today)].append(t)

    def box(key, title, items, *, section: str = "Active"):
        # Urgent / Desk / Any time: group by category then project (headers).
        lis = render_today_task_lis(items, today, titles, live, vurl=vurl)
        urg = key if key in URGENCY_BUCKETS else ""
        add = add_row_html(live=live, section=section, pick_domain=True, urgency=urg)
        return (f'<section class="box counted {key}" data-key="{key}"><h2>{title} <span class="n">{len(items)}</span></h2>'
                f'<ul>{lis}{add or ("<li class=empty>Nothing</li>" if not lis else "")}</ul></section>')

    def fold(key, title, items, *, section: str = "Active"):
        # Snoozed stays date-first (not a Today urgency box).
        lis = "".join(task_li(t, today, titles, live, vurl=vurl) for t in sorted(items, key=sort_key))
        urg = key if key in URGENCY_BUCKETS else ""
        add = add_row_html(live=live, section=section, pick_domain=True, urgency=urg)
        return (f'<section class="box info counted {key}" data-key="{key}"><details><summary><h2>{title} '
                f'<span class="n">{len(items)}</span></h2></summary><ul>{lis}{add}</ul></details></section>'
                if items else "")

    extras = ""
    cal = stubs.get("calendar") or []
    if cal and isinstance(cal[0], dict):
        extras += render_calendar(cal, today)
    elif cal:
        extras += '<section class="box info"><h2>Calendar</h2><ul>' + "".join(
            f'<li dir="auto">{md(x)}</li>' for x in cal) + "</ul></section>"
    if stubs.get("emails"):
        items = [x if isinstance(x, dict) else {"text": x} for x in stubs["emails"]]
        lis = "".join(
            f'<li dir="auto" data-domain="{normalize_domain(x.get("domain")) or "NONE"}">{md(x["text"])}</li>'
            for x in items)
        extras += f'<section class="box info"><h2>Emails to answer</h2><ul>{lis}</ul></section>'
    more = fold("snoozed", "Snoozed", boxes["snoozed"], section="Active")
    lists = render_task_lists(data.get("task_lists") or [], today, titles, live, vurl)
    proj_src = {lst["project"]: lst["source"] for lst in (data.get("task_lists") or [])
                if lst.get("project") and lst.get("source")}
    targets = [{"domain": lst["domain"], "kind": lst["kind"], "key": lst["key"],
                "title": titles.get(lst["project"], lst["title"]) if lst.get("project") else lst["title"],
                "src": lst["source"], "project": lst.get("project") or ""}
               for lst in (data.get("task_lists") or [])]
    targets_json = json.dumps(targets, ensure_ascii=False).replace("<", "\\u003c")
    live_js = (f'<script type="application/json" id="move-targets">{targets_json}</script>\n'
               + LIVE_JS) if live else ""

    order = {d: i for i, d in enumerate(DOMAIN_ORDER)}
    order[None] = len(DOMAIN_ORDER)
    cards = []
    for p in sorted(projects, key=lambda p: (order.get(p["domain"], len(DOMAIN_ORDER)), p["title"])):
        s = pstubs.get(p["slug"], {})
        stand = s.get("stand") or p["next_action"] or "No next action recorded."
        dom = f'<span class="chip {p["domain"].lower()}">{p["domain"]}</span>' if p["domain"] else ""
        # Every open task of the project, as a real task (controls in live mode); first LEAD_N shown.
        tasks = sorted(p["subtasks"] + list(p["open_tasks"]), key=sort_key)
        # Deduplicate by id
        seen_p, tasks_u = set(), []
        for t in tasks:
            if t["id"] in seen_p:
                continue
            seen_p.add(t["id"]); tasks_u.append(t)
        tasks = tasks_u
        li = lambda t: task_li(t, today, titles, live, show_proj=False, vurl=vurl)
        lead = "".join(li(t) for t in tasks[:LEAD_N])
        rest = (f'<details><summary class="meta">{len(tasks) - LEAD_N} more</summary><ul class="tl">'
                f'{"".join(li(t) for t in tasks[LEAD_N:])}</ul></details>' if len(tasks) > LEAD_N else "")
        padd = add_row_html(live=live, src=proj_src.get(p["slug"], ""),
                            domain=p["domain"] or "", project=p["slug"])
        task_block = (f'<ul class=tl>{lead}{padd}</ul>{rest}' if (lead or padd) else "")
        notes = (f'<a class="repo" href="{vurl}/memory/projects/{p["slug"]}.md">project notes</a>' if vurl else "")
        code = repo_links_html(p, live)
        cards.append(
            f'<article class="card" id="p-{p["slug"]}" data-key="p-{p["slug"]}" '
            f'data-domain="{p["domain"] or "NONE"}" data-project="{html.escape(p["slug"])}">'
            f'<header><h3>{html.escape(p["title"])}</h3>{dom}</header>'
            f'<p class="stand" dir="auto">{md(stand)}</p>'
            f'{task_block}{code}'
            f'<footer>{notes}<span class="meta">updated {p["updated"] or "?"}</span></footer></article>')

    headline = f'<p class="headline">{md(stubs["headline"])}</p>' if stubs.get("headline") else ""
    rv = render_review(review, by_id, titles, live) if review and review.get("items") else ""
    filt = render_filter_panel(data.get("task_lists") or [], projects, titles)
    # Today: row1 Urgent|Desk; row2 Any time full-width (multi-column CSS in TEMPLATE).
    today_boxes = (
        f'<div class="boxes today-row">'
        f'{box("urgent", "Urgent", boxes["urgent"])}'
        f'{box("desk", "On the desk", boxes["desk"])}</div>'
        f'<div class="boxes today-anytime">'
        f'{box("anytime", "Any time", boxes["anytime"])}</div>')
    return TEMPLATE.format(
        brand=DASHBOARD_BRAND, palette_light=PALETTE_LIGHT, palette_dark=PALETTE_DARK,
        category_css=CATEGORY_CSS,
        generated=data["generated_at"].replace("T", " "), headline=headline, review=rv,
        filt=filt, boxes=today_boxes, list_cap=LIST_CAP,
        lists=lists, extras=extras, more=more, cards="".join(cards), nproj=len(projects),
        live=live_js)


LEAD_N = 5
STATIC_DIR = Path(__file__).resolve().parent / "static"
STATIC_FILES = {"favicon.ico": "image/x-icon", "icon.svg": "image/svg+xml", "app.ico": "image/x-icon"}
STATIC_TYPES = {".js": "text/javascript; charset=utf-8", ".css": "text/css; charset=utf-8",
                ".html": "text/html; charset=utf-8", ".woff2": "font/woff2", ".svg": "image/svg+xml",
                ".ico": "image/x-icon", ".png": "image/png"}


def static_file(rel: str) -> Path | None:
    """A file under static/ (vendored JS/CSS/fonts, the Notes page). Nothing outside."""
    if not rel or "\\" in rel or any(p in ("", ".", "..") for p in rel.split("/")):
        return None
    f = STATIC_DIR / rel
    try:
        if not f.resolve().is_relative_to(STATIC_DIR.resolve()) or f.suffix.lower() not in STATIC_TYPES:
            return None
    except OSError:
        return None
    return f if f.is_file() else None


def code_todo_html(a: dict, live: bool) -> str:
    out = []
    for t in a.get("todos") or []:
        if t.get("error"):
            out.append(f'<li class="meta">code TODO not loaded: {html.escape(t["error"])}</li>')
            continue
        lis = []
        for i in t["items"]:
            box = '<input type="checkbox" class="cdone" title="Mark done (commits to GitHub)">' if live else ""
            chip = (f' <span class="cst {i["status"]}">{i["status"].replace("_", " ")}</span>'
                    if i["status"] != "open" else "")
            when = f' <span class="meta">{html.escape(i["updated"])}</span>' if i.get("updated") else ""
            title = i["title"] if len(i["title"]) <= 140 else i["title"][:137].rstrip() + "…"
            lis.append(f'<li data-repo="{html.escape(a["repo"])}" data-path="{html.escape(t["path"])}" '
                       f'data-key="{html.escape(i["key"])}">{box}<span class="ct" dir="auto">'
                       f'<a href="{html.escape(t["url"])}#L{i["line"]}" title="{html.escape(i["key"])}">'
                       f'{html.escape(title)}</a>{chip}{when}</span></li>')
        n = len(t["items"])
        out.append(f'<li><details class="ctodo"><summary><span class="meta">code TODO</span> '
                   f'<a class="repo" href="{html.escape(t["url"])}">{html.escape(t["path"])}</a> '
                   f'<span class="meta">· {n} open</span></summary>'
                   f'<ul class="ctl">{"".join(lis)}</ul></details></li>')
    return "".join(out)


def repo_links_html(p: dict, live: bool = False) -> str:
    """Code row of a project card: linked repos (branch, last commit, open PRs), related
    repos as plain links, Local lines as text. Empty when the note names none."""
    gh = "https://github.com/"
    rows = []
    for a in p.get("activity") or [{"repo": r} for r in p.get("repos", [])]:
        name, branch = split_repo(a["repo"])
        url = a.get("branch_url") or gh + name + (f"/tree/{branch}" if branch else "")
        label = html.escape(name) + (f' <span class="branch">{html.escape(branch)}</span>' if branch else "")
        if a.get("error"):
            info = "activity not loaded — press ↻ Update"
        else:
            bits = [f"last commit {a['last_commit']}" if a.get("last_commit") else ""]
            for x in a.get("pulls", []):
                bits.append(f'<a href="{html.escape(x["url"])}">PR #{x["number"]}</a>')
            info = " · ".join(b for b in bits if b)
        rows.append(f'<li><a class="repo" href="{html.escape(url)}">{label}</a>'
                    f'{f" <span class=meta>{info}</span>" if info else ""}</li>')
        rows.append(code_todo_html(a, live))
    for r in p.get("related", []):
        name, branch = split_repo(r)
        if REPO_RE.match(r):
            url = gh + name + (f"/tree/{branch}" if branch else "")
            label = html.escape(name) + (f' <span class="branch">{html.escape(branch)}</span>' if branch else "")
            rows.append(f'<li><span class="meta">related</span> <a class="repo" href="{html.escape(url)}">{label}</a></li>')
        else:
            rows.append(f'<li><span class="meta">related</span> {html.escape(r)}</li>')
    for l in p.get("local", []):
        rows.append(f'<li><span class="meta">local</span> <span class="local">{md(l)}</span></li>')
    return f'<ul class="code">{"".join(rows)}</ul>' if rows else ""


TEMPLATE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{brand}</title>
<link rel="icon" href="/favicon.ico" sizes="32x32">
<link rel="icon" href="/icon.svg" type="image/svg+xml">
<link rel="apple-touch-icon" href="/icon.svg">
<style>
:root {{ --bg:#faf9f5; --card:#fff; --text:#141413; --muted:#6b6a66; --border:#e5e4df;
  --accent:#c96442; --red:#b3261e; --red-bg:#fbeae8; --amber:#9a6a00; --amber-bg:#fbf3e0;
  {palette_light} }}
@media (prefers-color-scheme: dark) {{ :root:not([data-theme="light"]) {{
  --bg:#1c1b19; --card:#262522; --text:#ecebe6; --muted:#a3a29c; --border:#3a3935;
  --accent:#e08a6b; --red:#f2817a; --red-bg:#3a2321; --amber:#e3b45c; --amber-bg:#3a3020;
  {palette_dark} }} }}
* {{ box-sizing:border-box }}
body {{ margin:0; padding:20px 16px 40px; background:var(--bg); color:var(--text);
  font:14px/1.45 -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; }}
body.has-sel {{ padding-bottom:88px }}
main {{ max-width:1280px; margin:0 auto }}
h1 {{ font-size:1.4rem; margin:0 }}
.top {{ display:flex; justify-content:space-between; align-items:baseline; flex-wrap:wrap; gap:8px }}
.tabs {{ display:flex; gap:2px; background:var(--bg); border:1px solid var(--border); border-radius:8px; padding:2px;
  margin-right:auto; align-self:center }}
.tabs a {{ padding:3px 12px; border-radius:6px; color:var(--muted); text-decoration:none; font-weight:600; font-size:13px }}
.tabs a.on {{ background:var(--card); color:var(--text); box-shadow:0 1px 3px rgba(0,0,0,.15) }}
.gen, .meta {{ color:var(--muted); font-size:12px }}
.headline {{ margin:6px 0 16px; font-size:15px }}
.boxes {{ display:grid; grid-template-columns:repeat(auto-fit, minmax(300px, 1fr)); gap:12px; margin-bottom:12px }}
#tasks.today {{ display:flex; flex-direction:column; gap:12px; margin-bottom:12px }}
#tasks .today-row {{ display:grid; grid-template-columns:1fr 1fr; gap:12px; margin-bottom:0 }}
#tasks .today-anytime {{ display:block; margin-bottom:0 }}
#tasks .today-anytime .box.anytime {{ width:100% }}
@media (max-width:700px) {{ #tasks .today-row {{ grid-template-columns:1fr }} }}
.box {{ background:var(--card); border:1px solid var(--border); border-radius:10px; padding:12px 14px }}
.box.urgent {{ border-top:3px solid var(--red) }} .box.desk {{ border-top:3px solid var(--accent) }}
.box.anytime {{ border-top:3px solid var(--muted) }} .box.info {{ border-top:3px solid var(--c0) }}
.box.anytime > ul {{ columns:2; column-gap:1.5rem }}
@media (min-width:960px) {{ .box.anytime > ul {{ columns:3 }} }}
@media (max-width:600px) {{ .box.anytime > ul {{ columns:1 }} }}
.box.anytime > ul > li {{ break-inside:avoid; -webkit-column-break-inside:avoid; page-break-inside:avoid }}
h2 {{ font-size:13px; text-transform:uppercase; letter-spacing:.04em; margin:0 0 8px }}
.n {{ color:var(--muted); font-weight:400 }}
ul {{ list-style:none; margin:0; padding:0 }}
:is(.box, .tl) li {{ padding:5px 0; border-bottom:1px solid var(--border); display:flex; flex-wrap:wrap; gap:4px 6px; align-items:baseline }}
:is(.box, .tl) li:last-child {{ border-bottom:0 }} .empty {{ color:var(--muted) }}
.t {{ flex:1 1 180px }}
.chip {{ font-size:10px; font-weight:600; padding:1px 6px; border-radius:8px }}
{category_css}
.lists {{ margin:18px 0 8px }} .lists-h {{ font-size:13px; text-transform:uppercase; letter-spacing:.04em; margin:0 0 10px; color:var(--muted) }}
.nested {{ margin-top:8px; display:flex; flex-direction:column; gap:6px }}
.plist {{ border-top:1px solid var(--border); padding-top:6px }}
.plist summary h3 {{ font-size:13px; margin:0; display:inline; text-transform:none; letter-spacing:0 }}
.plist summary h3::before {{ content:"▸ "; color:var(--muted) }}
.plist[open] summary h3::before {{ content:"▾ " }}
.morebtn {{ font:inherit; font-size:12px; line-height:1.7; padding:4px 0; margin-top:4px; cursor:pointer;
  border:0; background:none; color:var(--accent); text-decoration:underline; text-underline-offset:2px }}
.morebtn:hover {{ filter:brightness(0.92) }}
li.cap-hide {{ display:none !important }}
ul.scrolly {{ overflow-y:auto; overscroll-behavior:contain; scrollbar-width:thin }}
ul.scrolly > li.add-row {{ position:sticky; bottom:0; background:var(--card); z-index:1 }}
.morehint {{ font-size:11px; margin-top:4px }}
.morehint.at-end {{ visibility:hidden }}
.pullbtn {{ font:inherit; font-size:12px; line-height:1.7; padding:0 10px; margin-right:6px; cursor:pointer;
  border:1px solid var(--border); border-radius:6px; background:var(--card); color:var(--text) }}
.pullbtn:hover {{ border-color:var(--accent) }} .pullbtn:disabled {{ opacity:.5; cursor:default }}
.present-btn {{ font:inherit; font-size:12px; line-height:1.7; padding:0 10px; margin-right:4px; cursor:pointer;
  border:1px solid var(--border); border-radius:6px; background:var(--card); color:var(--text) }}
.present-btn[aria-pressed="true"] {{ border-color:var(--accent); background:var(--accent); color:#fff; font-weight:600 }}
body.present .box.anytime {{ display:none !important }}
body.present .today-row .box.urgent, body.present .today-row .box.desk {{
  box-shadow:0 1px 0 rgba(0,0,0,.04); font-size:15px }}
body.present .today-row .box h2 {{ font-size:14px }}
body.present .headline {{ display:none }}
.badge {{ font-size:11px; padding:1px 6px; border-radius:8px; background:var(--bg); color:var(--muted); white-space:nowrap }}
.badge.over {{ background:var(--red-bg); color:var(--red); font-weight:600 }}
.badge.soon {{ background:var(--amber-bg); color:var(--amber) }}
.proj {{ font-size:11px; color:var(--muted) }}
a {{ color:var(--accent); text-decoration:none }} a:hover {{ text-decoration:underline }}
code {{ font-size:12px; background:var(--bg); padding:0 3px; border-radius:3px; overflow-wrap:anywhere }}
pre {{ position:relative; background:var(--bg); border-radius:4px; padding:6px 30px 6px 8px; margin:4px 0; white-space:pre-wrap }}
pre code {{ padding:0; background:none }} pre .cp {{ position:absolute; top:4px; right:4px }}
.cp {{ font:inherit; font-size:12px; line-height:1; padding:1px 4px; margin-left:3px; cursor:pointer; color:var(--muted);
  background:none; border:1px solid var(--border); border-radius:4px; vertical-align:1px }} .cp:hover {{ color:var(--text) }}
.prop li {{ display:flex; flex-wrap:wrap; gap:8px; align-items:baseline }} .prop li input {{ flex:1 1 100%; font:inherit; padding:4px 6px; border:1px solid var(--border); border-radius:4px; background:var(--bg); color:var(--text) }} .prop li .pt {{ flex:1 }} .prop .why {{ color:var(--muted) }}
.prop button {{ font:inherit; font-size:12px; padding:0 8px; cursor:pointer; border:1px solid var(--border);
  border-radius:4px; background:none; color:var(--text) }} .prop h3 {{ font-size:12px; color:var(--muted); margin:8px 0 2px }}
.proj-section h2 {{ margin:22px 0 10px }}
.cards {{ display:grid; grid-template-columns:repeat(auto-fill, minmax(340px, 1fr)); gap:12px }}
.card {{ background:var(--card); border:1px solid var(--border); border-radius:10px; padding:12px 14px; display:flex; flex-direction:column; gap:6px }}
.card header {{ display:flex; justify-content:space-between; gap:8px; align-items:baseline }}
h3 {{ font-size:15px; margin:0 }} h4 {{ font-size:11px; text-transform:uppercase; color:var(--muted); margin:6px 0 2px }}
.stand {{ margin:0 }}
.card li {{ padding:2px 0 2px 12px; position:relative }}
.card li::before {{ content:"•"; position:absolute; left:0; color:var(--muted) }}
.card footer {{ margin-top:auto; padding-top:6px; border-top:1px solid var(--border); display:flex; flex-wrap:wrap; gap:4px 10px; align-items:baseline }}
.repo {{ font-size:12px; font-weight:600 }}
.code {{ list-style:none; margin:6px 0 0; padding:0; font-size:12px; display:grid; gap:2px }}
.code .branch {{ font-family:ui-monospace,monospace; font-weight:400; padding:0 4px; border-radius:4px; background:var(--c0-bg); color:var(--c0) }}
.code .local code {{ font-size:11px }}
.ctodo summary {{ cursor:pointer; list-style-position:outside }}
.ctl {{ list-style:none; margin:4px 0 2px; padding:0 0 0 4px; display:grid; gap:3px }}
.ctl li {{ display:flex; gap:6px; align-items:baseline; line-height:1.35 }}
.ctl li.checked .ct {{ text-decoration:line-through; opacity:.6 }}
.ctl li.gone {{ opacity:0; transition:opacity .3s }}
.ctl input.cdone {{ margin:0; flex:none; accent-color:var(--accent); cursor:pointer; position:relative; top:2px }}
.ctl a {{ color:inherit }}
.cst {{ font-size:11px; padding:0 5px; border-radius:4px; background:var(--amber-bg); color:var(--amber) }}
.cst.blocked {{ background:var(--red-bg); color:var(--red) }}
.card ul.tl li {{ padding:4px 0 }} .card ul.tl li::before {{ content:none }}
.card details summary {{ margin-top:4px }}
.badge.old {{ background:var(--amber-bg); color:var(--amber) }}
.badge.new {{ background:var(--accent); color:var(--card); font-weight:600 }}
a.src {{ font-size:12px }}
.box.review {{ grid-column:1 / -1; border-top:3px solid var(--amber) }}
.review li.rvi {{ align-items:center }}
.why {{ flex-basis:100%; font-size:12px; color:var(--muted) }}
.rv {{ display:flex; flex-wrap:wrap; gap:4px; flex-basis:100% }}
.rv button {{ font:inherit; font-size:12px; line-height:1.7; padding:0 8px; cursor:pointer;
  border:1px solid var(--border); border-radius:6px; background:var(--bg); color:var(--text) }}
.rv button:hover {{ border-color:var(--accent) }}
.blurb a {{ overflow-wrap:anywhere }}
@media (max-width:400px) {{ .cards {{ grid-template-columns:1fr }} }}
[hidden] {{ display:none !important }}
/* Page = content + a sticky filter sidebar on the right (beside the lists, never over them).
   Sidebar: one block per domain, coloured header row, its projects indented on a coloured rule. */
.layout {{ display:grid; grid-template-columns:minmax(0, 1fr) 232px; gap:20px; max-width:1540px;
  margin:0 auto; align-items:start }}
.layout > main {{ max-width:none; margin:0; min-width:0 }}
.side {{ position:sticky; top:12px }}
.filt {{ display:flex; flex-direction:column; gap:10px; font-size:12px; color:var(--text);
  background:var(--card); border:1px solid var(--border); border-radius:10px; padding:10px;
  box-shadow:0 2px 10px rgba(0,0,0,.12); max-height:calc(100vh - 24px); overflow:auto; box-sizing:border-box }}
.filt-head {{ display:flex; align-items:center; gap:8px; flex-wrap:wrap }}
.filt-label {{ color:var(--muted); font-size:11px; text-transform:uppercase; letter-spacing:.04em }}
.filt-hint {{ color:var(--muted); font-size:10.5px; flex-basis:100%; line-height:1.3 }}
.filt-table {{ display:flex; flex-direction:column; gap:10px }}
.filt-cat {{ --c:var(--muted); --cb:var(--bg); display:flex; flex-direction:column; gap:3px; min-width:0 }}
.filt label {{ display:flex; gap:6px; align-items:flex-start; cursor:pointer; line-height:1.3; user-select:none }}
.filt-dom {{ font-weight:700; font-size:12.5px; letter-spacing:.02em; color:var(--c); background:var(--cb);
  border-radius:6px; padding:4px 7px }}
.filt-dom input {{ accent-color:var(--c) }} .filt-dom span {{ overflow-wrap:anywhere }}
.filt-kids {{ display:flex; flex-direction:column; gap:2px; margin:0 0 0 12px; padding-left:9px;
  border-left:2px solid var(--c) }}
.filt-kids:empty {{ display:none }}
.filt-proj {{ font-size:12px; color:var(--text); opacity:.85; padding:1px 0 }}
.filt-proj input {{ margin-top:2px; accent-color:var(--c) }}
.filt label:hover {{ opacity:1 }} .filt-proj:hover {{ color:var(--c) }}
.filt label.solo {{ outline:2px solid var(--accent); outline-offset:1px; border-radius:6px; opacity:1 }}
@media (max-width:900px) {{
  .layout {{ grid-template-columns:minmax(0, 1fr) }}
  .side {{ position:static; order:-1 }}
  .filt {{ max-height:none; box-shadow:none }}
  .filt-table {{ flex-direction:row; flex-wrap:wrap; gap:10px 18px }}
}}
.cal-box {{ grid-column:1 / -1 }}
.cal {{ display:grid; grid-template-columns:30px repeat(var(--days), minmax(0, 1fr)); column-gap:6px; font-size:12px }}
.cal-head {{ font-weight:600; padding-bottom:4px }} .cal-head span {{ color:var(--muted); font-weight:400; margin-left:6px }}
.cal-allday {{ display:flex; flex-direction:column; gap:2px; padding-bottom:4px }}
.cal-hours {{ position:relative }}
.cal-hour {{ position:absolute; right:2px; transform:translateY(-6px); color:var(--muted); font-size:10px }}
.cal-col {{ position:relative; border-left:1px solid var(--border);
  background:repeating-linear-gradient(to bottom, var(--border) 0 1px, transparent 1px var(--hour)) }}
.ev {{ position:absolute; border-radius:6px; color:#fff; padding:2px 5px; overflow:hidden; line-height:1.25; font-size:11.5px }}
.ev b {{ display:block; font-weight:600 }} .ev span {{ opacity:.85; font-size:10.5px }}
.ev.ad {{ position:static; border-radius:4px }}
.cal-now {{ position:absolute; left:-4px; right:0; height:2px; background:var(--red); pointer-events:none }}
details summary {{ cursor:pointer; list-style:none }} details summary::-webkit-details-marker {{ display:none }}
details summary h2 {{ display:inline }} details summary h2::before {{ content:"▸ "; color:var(--muted) }}
details[open] summary h2::before {{ content:"▾ " }} details[open] summary {{ margin-bottom:8px }}
input.done {{ margin:0; flex:none; align-self:center; accent-color:var(--accent); cursor:pointer }}
li.checked {{ opacity:.45; transition:opacity .3s }} li.checked .t {{ text-decoration:line-through }}
li.gone {{ opacity:0 }} li.working {{ opacity:.45; pointer-events:none }}
li[data-id] {{ position:relative }}
.acts {{ display:none; gap:3px }}
@media (hover:hover) {{ li:hover > .acts {{ display:flex; position:absolute; right:0; top:3px; background:var(--card);
  padding-left:6px; box-shadow:-8px 0 8px var(--card) }} }}
li.open > .acts {{ display:flex; position:static; flex-basis:100%; justify-content:flex-end }}
.acts button, .blurb button, .menu button {{ font:inherit; font-size:12px; line-height:1.7; padding:0 6px; cursor:pointer;
  border:1px solid var(--border); border-radius:6px; background:var(--bg); color:var(--text) }}
li[data-id].sel {{ background:var(--amber-bg); border-radius:6px; outline:2px solid var(--amber) }}
.selbar {{ position:fixed; left:0; right:0; bottom:0; z-index:40; display:flex; flex-wrap:wrap;
  gap:10px; align-items:center; justify-content:center; padding:12px 16px calc(12px + env(safe-area-inset-bottom, 0px));
  background:var(--amber-bg); border-top:2px solid var(--amber); box-shadow:0 -6px 24px rgba(0,0,0,.14);
  font-size:14px }}
.selbar[hidden] {{ display:none !important }}
.selbar .seln {{ color:var(--text); font-weight:600; min-width:6.5rem }}
.selbar .selhint {{ color:var(--muted); font-size:12px }}
.selbar button {{ font:inherit; font-size:13px; line-height:1.8; padding:4px 14px; cursor:pointer;
  border:1px solid var(--border); border-radius:8px; background:var(--card); color:var(--text) }}
.selbar button.primary {{ border-color:var(--accent); background:var(--accent); color:#fff; font-weight:700;
  padding:4px 18px }}
.selbar button.primary:hover {{ filter:brightness(1.05) }}
.selbar button:disabled {{ opacity:.45; cursor:not-allowed; filter:none }}
li.ghead {{ list-style:none; border-bottom:0 !important; padding:10px 0 2px !important;
  font-size:11px; font-weight:700; letter-spacing:.04em; text-transform:uppercase;
  color:var(--muted); opacity:1 !important; pointer-events:none; display:block }}
li.ghead.gdom {{ color:var(--text); font-size:12px; padding-top:14px !important }}
li.ghead.gproj {{ font-weight:600; text-transform:none; letter-spacing:0; padding-top:6px !important }}
li.ghead:first-child {{ padding-top:2px !important }}
:is(.box, .tl) li.blurb {{ background:var(--c0-bg); border-radius:6px; padding:6px 8px; margin:2px 0 4px; font-size:13px; align-items:center }}
.blurb[data-state=error] {{ background:var(--red-bg) !important; color:var(--red) }}
.blurb .bt {{ flex:1 1 200px }} .blurb .bb {{ display:flex; gap:4px }}
.blurb input {{ flex:1 1 100%; font:inherit; padding:4px 6px; border:1px solid var(--border);
  border-radius:6px; background:var(--card); color:var(--text) }}
li.add-row {{ border-bottom:0 !important; align-items:center !important; gap:8px !important; padding-top:8px !important;
  margin-top:2px; opacity:1 !important; pointer-events:auto }}
li.add-row.working {{ opacity:.45 !important; pointer-events:none }}
.add-check {{ flex:none; width:14px; height:14px; border:1.5px solid var(--muted); border-radius:3px; opacity:.5 }}
.add-inp {{ flex:1 1 120px; min-width:0; font:inherit; border:0; background:transparent; color:var(--text);
  padding:4px 0; outline:none }}
.add-inp::placeholder {{ color:var(--muted) }}
.add-dom {{ flex:none; font:inherit; font-size:11px; border:1px solid var(--border); border-radius:6px;
  background:var(--bg); color:var(--text); padding:1px 4px; max-width:7.5rem }}
.card li.add-row {{ padding-left:0 }} .card li.add-row::before {{ content:none }}
.spin {{ width:12px; height:12px; flex:none; border:2px solid var(--border); border-top-color:var(--accent);
  border-radius:50%; animation:spin .8s linear infinite }}
@keyframes spin {{ to {{ transform:rotate(360deg) }} }}
.menu {{ position:absolute; z-index:50; display:flex; flex-direction:column; gap:2px; min-width:140px; padding:4px;
  background:var(--card); border:1px solid var(--border); border-radius:8px; box-shadow:0 4px 14px rgba(0,0,0,.18) }}
.menu button {{ text-align:left; border:0; background:none; padding:3px 8px }}
.menu button:hover {{ background:var(--bg) }} .menu button.on {{ font-weight:700 }}
.picker {{ position:absolute; z-index:51; font:inherit }}
.picker.proj-new {{ display:flex; flex-direction:column; gap:6px; min-width:220px; padding:10px;
  background:var(--card); border:1px solid var(--border); border-radius:8px;
  box-shadow:0 4px 14px rgba(0,0,0,.18) }}
.picker.proj-new label {{ display:flex; flex-direction:column; gap:4px; font-size:12px; color:var(--muted) }}
.picker.proj-new input {{ font:inherit; font-size:14px; padding:6px 8px; border:1px solid var(--border);
  border-radius:6px; background:var(--bg); color:var(--text) }}
.picker.proj-new button {{ font:inherit; font-size:13px; padding:4px 10px; cursor:pointer;
  border:1px solid var(--border); border-radius:6px; background:var(--bg); color:var(--text) }}
.picker.proj-new button.ok {{ border-color:var(--accent); background:var(--accent); color:#fff; font-weight:700 }}
.toasts {{ position:fixed; left:50%; bottom:64px; transform:translateX(-50%); z-index:45; display:flex;
  flex-direction:column; gap:6px; align-items:center; width:max-content; max-width:90vw }}
body.has-sel .toasts {{ bottom:96px }}
.toast {{ background:var(--text); color:var(--bg); padding:8px 14px; border-radius:8px; font-size:13px }}
.toast button {{ margin-left:10px; background:none; border:0; color:var(--accent); font:inherit; font-weight:700; cursor:pointer }}
.updbtn {{ font:inherit; font-size:12px; line-height:1.7; padding:0 10px; margin-right:auto; cursor:pointer;
  border:1px solid var(--border); border-radius:6px; background:var(--card); color:var(--text) }}
.updbtn:hover {{ border-color:var(--accent) }} .updbtn:disabled {{ opacity:.5; cursor:default }}
.upd {{ margin:10px 0 0 }} .upd h2 {{ display:flex; gap:8px; align-items:center }}
.upd[data-state=error] {{ border-top-color:var(--red) }}
.upd .us {{ margin:0 0 8px }} .upd .us p {{ margin:6px 0 2px }} .upd .us ul, .upd .us ol {{ margin:0 0 6px; padding-left:20px }} .upd .us ul {{ list-style:disc }}
.upd .us li {{ display:list-item; padding:1px 0; border:0 }}
.upd .ux {{ font:inherit; font-size:12px; padding:0 8px; cursor:pointer; border:1px solid var(--border);
  border-radius:6px; background:var(--bg); color:var(--text) }}
.cal-now::before {{ content:""; position:absolute; left:0; top:-3px; width:8px; height:8px; border-radius:50%; background:var(--red) }}
</style></head>
<body><div class="layout"><main>
<div class="top"><h1>{brand}</h1>
<nav class="tabs"><a class="on" href="/">Tasks</a><a href="/notes">Notes</a></nav>
<span class="gen">generated {generated}</span></div>
{headline}
<div class="boxes" id="review">{review}</div>
<div class="today" id="tasks">{boxes}</div>
{lists}
<div class="boxes">{extras}</div>
<div class="boxes" id="more">{more}</div>
<div class="proj-section"><h2>Projects <span class="n">{nproj} active</span></h2><div class="cards" id="cards">{cards}</div></div>
</main>
<aside class="side">{filt}</aside></div>
<script>
(() => {{
  const KEY = "dashboard-filter-v2";
  const LEGACY_KEY = "dashboard-hidden-domains";
  const PRESENT_KEY = "dashboard-present";
  const CAP = {list_cap};
  const normDom = v => v;

  function loadState() {{
    try {{
      const raw = JSON.parse(localStorage.getItem(KEY) || "null");
      if (raw && typeof raw === "object")
        return {{
          offDomains: (raw.offDomains || []).map(normDom),
          offProjects: raw.offProjects || [],
        }};
    }} catch (e) {{}}
    // Migrate flat domain chips → v2.
    let legacy = [];
    try {{ legacy = JSON.parse(localStorage.getItem(LEGACY_KEY) || "[]"); }} catch (e) {{}}
    return {{ offDomains: (legacy || []).map(normDom), offProjects: [] }};
  }}
  function saveState(offDomains, offProjects) {{
    try {{
      localStorage.setItem(KEY, JSON.stringify({{ offDomains, offProjects }}));
    }} catch (e) {{}}
  }}
  // Double-click "solo": show one domain or one project only. `solo` remembers which, plus
  // a snapshot of every checkbox, so it stays in force only while nothing else was changed.
  const SOLO_KEY = "dashboard-filter-solo";
  let solo = null;
  try {{ solo = JSON.parse(localStorage.getItem(SOLO_KEY) || "null"); }} catch (e) {{}}
  const snap = () => [...document.querySelectorAll("#filt input[type=checkbox]")]
    .map(i => i.checked ? 1 : i.indeterminate ? 2 : 0).join("");
  const soloActive = () => !!solo && solo.snap === snap();
  function setSolo(v) {{
    solo = v;
    try {{ v ? localStorage.setItem(SOLO_KEY, JSON.stringify(v)) : localStorage.removeItem(SOLO_KEY); }} catch (e) {{}}
  }}
  function syncParent(cat) {{
    const dom = cat.querySelector('input[data-role="dom"]');
    const kids = [...cat.querySelectorAll('input[data-role="proj"]')];
    if (!dom) return;
    if (!kids.length) {{ dom.indeterminate = false; return; }}
    const nOn = kids.filter(k => k.checked).length;
    dom.checked = nOn === kids.length;
    dom.indeterminate = nOn > 0 && nOn < kids.length;
  }}
  function restore() {{
    const st = loadState();
    document.querySelectorAll(".filt-cat").forEach(cat => {{
      const dom = cat.querySelector('input[data-role="dom"]');
      const kids = [...cat.querySelectorAll('input[data-role="proj"]')];
      if (!dom) return;
      const d = normDom(dom.value);
      if (st.offDomains.includes(d)) {{
        dom.checked = false; dom.indeterminate = false;
        kids.forEach(k => {{ k.checked = false; }});
      }} else {{
        kids.forEach(k => {{ k.checked = !st.offProjects.includes(k.value); }});
        syncParent(cat);
      }}
    }});
  }}
  function domainOff(d, offDomains) {{
    d = normDom(d);
    return offDomains.includes(d);
  }}
  function apply() {{
    const offDomains = [], offProjects = [];
    document.querySelectorAll(".filt-cat").forEach(cat => {{
      const dom = cat.querySelector('input[data-role="dom"]');
      const kids = [...cat.querySelectorAll('input[data-role="proj"]')];
      if (!dom) return;
      const d = normDom(dom.value);
      if (!dom.checked && !dom.indeterminate) offDomains.push(d);
      kids.forEach(k => {{ if (!k.checked) offProjects.push(k.value); }});
    }});
    saveState(offDomains, offProjects);
    document.querySelectorAll("[data-domain]").forEach(el => {{
      if (el.closest("#filt")) return;  // never hide the filter's own checkboxes
      const d = el.dataset.domain || "";
      const p = el.dataset.project || "";
      let hide = domainOff(d, offDomains);
      if (!hide && p && offProjects.includes(p)) hide = true;
      // Project solo: that domain's general (no-project) tasks are hidden too.
      if (!hide && !p && el.matches("li[data-id]") && soloActive() && solo.kind === "proj"
          && normDom(d) === solo.dom) hide = true;
      el.hidden = hide;
    }});
    document.querySelectorAll(".box.counted").forEach(b => {{
      const n = b.querySelector(":scope > h2 .n, :scope > details > summary h2 .n");
      if (!n) return;
      // Count real tasks only (skip group headers / Keep-add rows).
      n.textContent = b.querySelectorAll("li[data-id]:not([hidden])").length;
    }});
    document.querySelectorAll(".plist").forEach(pl => {{
      const n = pl.querySelector("summary h3 .n");
      if (!n) return;
      n.textContent = pl.querySelectorAll("li[data-id]:not([hidden])").length;
    }});
    applyCaps();
  }}
  function onFiltChange(ev) {{
    const inp = ev.target;
    if (!(inp instanceof HTMLInputElement)) return;
    const cat = inp.closest(".filt-cat");
    if (!cat) return;
    if (inp.dataset.role === "dom") {{
      inp.indeterminate = false;
      cat.querySelectorAll('input[data-role="proj"]').forEach(k => {{ k.checked = inp.checked; }});
    }} else if (inp.dataset.role === "proj") {{
      syncParent(cat);
    }}
    apply();
  }}
  document.getElementById("filt")?.addEventListener("change", onFiltChange);
  document.getElementById("filt")?.addEventListener("dblclick", ev => {{
    const lab = ev.target.closest(".filt label");
    const inp = lab?.querySelector("input[type=checkbox]");
    if (!inp) return;
    ev.preventDefault();
    getSelection()?.removeAllRanges();
    const kind = inp.dataset.role, dom = normDom(inp.dataset.domain || inp.value);
    const all = [...document.querySelectorAll("#filt input[type=checkbox]")];
    document.querySelectorAll("#filt label.solo").forEach(l => l.classList.remove("solo"));
    if (soloActive() && solo.kind === kind && solo.value === inp.value) {{
      all.forEach(i => {{ i.checked = true; i.indeterminate = false; }});   // again: show everything
      setSolo(null);
    }} else {{
      all.forEach(i => {{ i.checked = false; i.indeterminate = false; }});
      const cat = inp.closest(".filt-cat");
      if (kind === "dom") cat.querySelectorAll("input").forEach(i => {{ i.checked = true; }});
      else {{ inp.checked = true; syncParent(cat); }}
      setSolo({{ kind, dom, value: inp.value, snap: snap() }});
      lab.classList.add("solo");
    }}
    apply();
  }});

  // Long lists scroll inside their box: about CAP rows tall, with half of the next row
  // peeking out so it is clear there is more. The add row stays pinned at the bottom.
  function applyCaps() {{
    document.querySelectorAll(".box.counted[data-key], .plist[data-key]").forEach(box => {{
      const ul = box.querySelector(":scope > ul");
      if (!ul) return;
      box.querySelectorAll(":scope > .morebtn, :scope > .morehint").forEach(b => b.remove());
      ul.classList.remove("scrolly"); ul.style.maxHeight = "";
      const items = [...ul.children].filter(li => li.matches("li[data-id]") && !li.hidden);
      items.forEach(li => li.classList.remove("cap-hide"));
      if (items.length <= CAP) return;
      if (!items[CAP].getBoundingClientRect().height) return;   // hidden (filtered / collapsed)
      const top = ul.getBoundingClientRect().top;
      const last = items[CAP - 1].getBoundingClientRect(), peek = items[CAP].getBoundingClientRect();
      const add = ul.querySelector(":scope > li.add-row");
      const addH = add ? add.getBoundingClientRect().height : 0;
      ul.style.maxHeight = Math.round(last.bottom - top + peek.height / 2 + addH) + "px";
      ul.classList.add("scrolly");
      const hint = document.createElement("div");
      hint.className = "morehint meta";
      hint.textContent = "↓ scroll for " + (items.length - CAP) + " more";
      ul.after(hint);
      const sync = () => hint.classList.toggle("at-end", ul.scrollTop + ul.clientHeight >= ul.scrollHeight - 4);
      ul.onscroll = sync; sync();
    }});
  }}
  window.addEventListener("resize", () => {{ clearTimeout(window._capT); window._capT = setTimeout(applyCaps, 150); }});

  function setPresent(on) {{
    document.body.classList.toggle("present", on);
    const btn = document.getElementById("presentBtn");
    if (btn) {{
      btn.setAttribute("aria-pressed", on ? "true" : "false");
      btn.textContent = on ? "Present · on" : "Present";
    }}
    try {{ localStorage.setItem(PRESENT_KEY, on ? "1" : "0"); }} catch (e) {{}}
  }}
  document.getElementById("presentBtn")?.addEventListener("click", () => {{
    setPresent(!document.body.classList.contains("present"));
  }});
  let presentOn = false;
  try {{ presentOn = localStorage.getItem(PRESENT_KEY) === "1"; }} catch (e) {{}}
  setPresent(presentOn);

  function nowLine() {{
    const now = new Date();
    const iso = now.getFullYear() + "-" + String(now.getMonth() + 1).padStart(2, "0") + "-" + String(now.getDate()).padStart(2, "0");
    const col = document.querySelector(`.cal-col[data-date="${{iso}}"]`);
    if (!col) return;
    const cal = col.closest(".cal"), h0 = +cal.dataset.h0;
    const hour = parseFloat(getComputedStyle(cal).getPropertyValue("--hour"));
    const top = (now.getHours() + now.getMinutes() / 60 - h0) * hour;
    if (top < 0 || top > col.offsetHeight) return;
    const line = document.createElement("div");
    line.className = "cal-now"; line.style.top = top + "px"; col.appendChild(line);
  }}
  restore();
  if (soloActive()) [...document.querySelectorAll("#filt input[type=checkbox]")]
    .find(i => i.dataset.role === solo.kind && i.value === solo.value)?.closest("label")?.classList.add("solo");
  apply(); nowLine();
  window.applyFilter = apply;
  window.applyCaps = applyCaps;
}})();
</script>{live}
</body></html>
"""


LIVE_JS = r"""<div class="toasts"></div>
<script>
(() => {
  const DONE_DELAY = 5000;
  let checking = 0, shownAt = Date.now();
  const $ = (sel, el = document) => el.querySelector(sel);
  const sleep = ms => new Promise(r => setTimeout(r, ms));
  const esc = s => String(s).replace(/[&<>"]/g, c => ({"&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;"}[c]));
  // Inline markdown (code, bold, links), same subset as md() on the server.
  const CP = '<button class="cp" title="Copy">⧉</button>';
  const mdi = s => esc(s).replace(/``\s?(.+?)\s?``|`([^`]+)`/g, (_, a, b) => {
      const c = a || b; return `<code>${c}</code>` + (/\s/.test(c.trim()) || c.length > 24 ? CP : ""); })
    .replace(/\*\*([^*]+)\*\*/g, "<b>$1</b>")
    .replace(/(^|[^"])(https?:\/\/[^\s<]+)/g, '$1<a href="$2">$2</a>');
  // Block markdown for Claude's replies: paragraphs, bullet and numbered lists, headings.
  function mdb(s) {
    const out = []; let list = null, fence = null;
    const close = () => { if (list) { out.push(`</${list}>`); list = null; } };
    for (const line of String(s).split("\n")) {
      if (/^\s*```/.test(line)) {  // fenced block: copy button
        if (fence === null) { close(); fence = []; }
        else { out.push(`<pre><code>${esc(fence.join("\n"))}</code>${CP}</pre>`); fence = null; }
        continue;
      }
      if (fence !== null) { fence.push(line); continue; }
      const m = /^\s*(?:([-*•])|(\d+)[.)])\s+(.*)$/.exec(line);
      if (m) {
        const tag = m[1] ? "ul" : "ol";
        if (list !== tag) { close(); out.push(`<${tag}>`); list = tag; }
        out.push(`<li dir="auto">${mdi(m[3])}</li>`);
      } else {
        close();
        const h = /^\s*#{1,6}\s+(.*)$/.exec(line);
        if (h) out.push(`<p dir="auto"><b>${mdi(h[1])}</b></p>`);
        else if (line.trim()) out.push(`<p dir="auto">${mdi(line)}</p>`);
      }
    }
    close();
    if (fence !== null) out.push(`<pre><code>${esc(fence.join("\n"))}</code>${CP}</pre>`);
    return out.join("");
  }
  document.addEventListener("click", async ev => {
    const b = ev.target.closest(".cp");
    if (!b) return;
    ev.preventDefault(); ev.stopPropagation();
    const text = b.previousElementSibling.textContent;
    try { await navigator.clipboard.writeText(text); toast("Copied"); }
    catch (e) { toast(copyFallback(text) ? "Copied" : "Copy failed: " + e.message); }
  }, true);
  // Clipboard API is refused in some contexts (desktop-app pane, plain http on a LAN IP).
  function copyFallback(text) {
    const t = document.createElement("textarea");
    t.value = text; t.setAttribute("readonly", "");
    t.style.cssText = "position:fixed;top:0;left:0;opacity:0";
    document.body.appendChild(t); t.select(); t.setSelectionRange(0, text.length);
    let ok = false; try { ok = document.execCommand("copy"); } catch (_) {}
    t.remove(); return ok;
  }
  async function post(url, body) {
    const r = await fetch(url, {method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify(body)});
    const j = await r.json().catch(() => ({error: "HTTP " + r.status}));
    if (!r.ok) throw new Error(j.error || r.status);
    return j;
  }
  function toast(msg, commit, undoUrl = "/undo") {
    const d = document.createElement("div");
    d.className = "toast"; d.textContent = msg;
    if (commit) {
      const u = document.createElement("button");
      u.textContent = "Undo";
      u.onclick = async () => {
        d.remove();
        try { await post(undoUrl, {commit}); toast("Undone"); await refresh(); }
        catch (e) { toast("Can't undo: " + e.message); }
      };
      d.append(u);
    }
    $(".toasts").append(d);
    setTimeout(() => d.remove(), commit ? 10000 : 6000);
  }

  // Re-render the task boxes in place.
  async function refresh() {
    while (checking) await sleep(300);
    const doc = new DOMParser().parseFromString(await (await fetch("/")).text(), "text/html");
    const open = [...document.querySelectorAll("details[open]")].map(d => d.closest("[data-key]")?.dataset.key);
    for (const id of ["review", "tasks", "more", "cards", "lists"]) {
      const cur = $("#" + id), next = $("#" + id, doc);
      if (cur && next) cur.replaceWith(next);
      else if (cur && !next) cur.remove();
      else if (!cur && next) {
        const anchor = $("#tasks");
        if (id === "lists" && anchor) anchor.after(next);
      }
    }
    open.forEach(k => { const d = k && $(`[data-key="${k}"] details`); if (d) d.open = true; });
    window.applyFilter();
    if (typeof window.applyCaps === "function") window.applyCaps();
    shownAt = Date.now();
    if (typeof paintSel === "function") paintSel();
  }
  async function pullRefresh() {
    pb.disabled = true; pb.textContent = "Pulling…";
    try {
      await post("/pull", {});
      await refresh();
      toast("Vault pulled");
    } catch (e) { toast("Pull failed: " + e.message); }
    finally { pb.disabled = false; pb.textContent = "↻ Pull"; }
  }

  // ---- buttons and menus
  const iso = d => d.getFullYear() + "-" + String(d.getMonth() + 1).padStart(2, "0") + "-" + String(d.getDate()).padStart(2, "0");
  const plus = n => { const d = new Date(); d.setDate(d.getDate() + n); return iso(d); };
  const nextSunday = () => { const d = new Date(); d.setDate(d.getDate() + (7 - d.getDay())); return iso(d); };
  const closeMenus = () => document.querySelectorAll(".menu, .picker").forEach(m => m.remove());
  function place(el, anchor) {
    const r = anchor.getBoundingClientRect();
    document.body.append(el);
    // Open above the anchor when there is not enough room below (selbar is bottom-fixed).
    const h = el.offsetHeight || 0;
    let top = r.bottom + scrollY + 4;
    if (r.bottom + h + 8 > innerHeight) top = Math.max(scrollY + 8, r.top + scrollY - h - 4);
    el.style.top = top + "px";
    el.style.left = Math.max(8, Math.min(r.left + scrollX, scrollX + innerWidth - el.offsetWidth - 8)) + "px";
  }
  function menu(btn, items) {
    closeMenus();
    const m = document.createElement("div");
    m.className = "menu";
    for (const [label, fn, on] of items) {
      const x = document.createElement("button");
      x.textContent = label; if (on) x.className = "on";
      x.onclick = e => { e.stopPropagation(); m.remove(); fn(); };
      m.append(x);
    }
    place(m, btn);
  }
  function pickDate(btn, cur, fn) {
    closeMenus();
    const inp = document.createElement("input");
    inp.type = "date"; inp.className = "picker"; inp.value = cur || plus(1);
    place(inp, btn);
    inp.onchange = () => { if (inp.value) { inp.remove(); fn(inp.value); } };
    try { inp.showPicker(); } catch (e) { inp.focus(); }
  }
  async function act(li, op, arg = "") {
    li.classList.add("working");
    try {
      const j = await post("/act", {src: li.dataset.src, id: li.dataset.id, op, arg, review: li.dataset.rid});
      toast(j.label, j.commit);
      if (j.warn) toast(j.warn);
      await refresh();
    } catch (e) { li.classList.remove("working"); toast("Not saved: " + e.message); }
  }
  document.addEventListener("click", e => {
    const b = e.target.closest(".acts button");
    if (!b) {
      // Exclude .selbar: Move… opens a menu on the same click that would otherwise
      // bubble here and closeMenus() immediately (classic open-then-close no-op).
      if (!e.target.closest(".menu, .picker, .selbar")) closeMenus();
      const t = e.target.closest("li[data-id] > .t");
      // Skip open-toggle while multi-selecting (modifiers or sticky selection mode).
      const selecting = e.shiftKey || e.metaKey || e.ctrlKey
        || document.body.classList.contains("has-sel");
      if (t && !selecting && !matchMedia("(hover: hover)").matches && !e.target.closest("a"))
        t.parentElement.classList.toggle("open");
      return;
    }
    e.stopPropagation();
    const li = b.closest("li"), d = li.dataset;
    const pick = (cur, op) => ["Pick date…", () => pickDate(b, cur, v => act(li, op, v))];
    if (b.dataset.a === "due") menu(b, [pick(d.due, "due"), ...(d.due ? [["Clear", () => act(li, "due")]] : [])]);
    if (b.dataset.a === "snooze") menu(b, [["Tomorrow", () => act(li, "snooze", plus(1))],
      ["Next Sunday", () => act(li, "snooze", nextSunday())], ["+1 week", () => act(li, "snooze", plus(7))],
      pick(d.sched, "snooze"), ...(d.sched ? [["Unsnooze", () => act(li, "snooze")]] : [])]);
    if (b.dataset.a === "urgency") {
      const cur = d.bucket === "snoozed" ? "anytime" : (d.bucket || "anytime");
      menu(b, [["Any time", "anytime"], ["On the desk", "desk"], ["Urgent", "urgent"]].map(
        ([label, u]) => [label, () => act(li, "urgency", u), cur === u]));
    }
    if (b.dataset.a === "src") { closeMenus(); showSource(li); }
  });

  // ---- multi-select → Move… / Project… (not always-visible row control)
  let targets = [];
  try { targets = JSON.parse($("#move-targets")?.textContent || "[]"); } catch (_) { targets = []; }
  const selected = new Map(); // key src\\0id → {src,id,domain}
  const selKey = li => `${li.dataset.src}\0${li.dataset.id}`;
  const selBar = document.createElement("div");
  selBar.className = "selbar"; selBar.hidden = true;
  selBar.innerHTML = '<span class="seln">0 selected</span>'
    + '<span class="selhint">Done, urgency, move, or project</span>'
    + '<button type="button" data-s="done">Done</button>'
    + '<button type="button" data-s="urgency">Urgency…</button>'
    + '<button type="button" class="primary" data-s="move">Move…</button>'
    + '<button type="button" data-s="project" disabled title="Select tasks from one category">Project…</button>'
    + '<button type="button" data-s="clear">Clear</button>';
  document.body.append(selBar);
  function selectionDomain() {
    const doms = new Set();
    for (const x of selected.values()) {
      const d = x.domain && x.domain !== "NONE" ? x.domain : "";
      if (!d) return null;
      doms.add(d);
    }
    return doms.size === 1 ? [...doms][0] : null;
  }
  function paintSel() {
    document.querySelectorAll("li[data-id].sel").forEach(li => {
      if (!selected.has(selKey(li))) li.classList.remove("sel");
    });
    for (const {src, id} of selected.values()) {
      document.querySelectorAll(`li[data-id="${CSS.escape(id)}"]`).forEach(li => {
        if (li.dataset.src === src) li.classList.add("sel");
      });
    }
    const n = selected.size;
    selBar.querySelector(".seln").textContent = n
      ? `${n} selected` : "0 selected";
    selBar.hidden = n === 0;
    document.body.classList.toggle("has-sel", n > 0);
    const projBtn = selBar.querySelector('[data-s="project"]');
    if (projBtn) {
      const mixed = n > 0 && !selectionDomain();
      const dom = selectionDomain();
      projBtn.disabled = !dom;
      projBtn.title = !n ? "Select tasks from one category"
        : mixed ? "Mixed categories — select one category only"
        : `Projects in ${dom}`;
    }
  }
  function clearSel() { selected.clear(); paintSel(); }
  function toggleSel(li) {
    if (!li?.dataset?.id || !li.dataset.src) return;
    const k = selKey(li);
    if (selected.has(k)) selected.delete(k);
    else selected.set(k, {src: li.dataset.src, id: li.dataset.id,
      domain: li.dataset.domain || ""});
    paintSel();
  }
  document.addEventListener("click", e => {
    if (e.target.closest(".acts, .menu, .picker, .selbar, input.done, a, .add-row, .rv")) return;
    const li = e.target.closest("li[data-id]");
    if (!li) return;
    // Modifier-click starts selection; once anything is selected, plain click toggles too.
    if (e.shiftKey || e.metaKey || e.ctrlKey || selected.size > 0) {
      e.preventDefault();
      toggleSel(li);
    }
  });
  document.addEventListener("keydown", e => {
    if (e.key === "Escape" && selected.size) clearSel();
  });
  let pressTimer = null, pressLi = null;
  document.addEventListener("pointerdown", e => {
    if (e.pointerType === "mouse" || e.target.closest(".acts, input.done, a, .add-row, .selbar")) return;
    const li = e.target.closest("li[data-id]");
    if (!li) return;
    pressLi = li;
    pressTimer = setTimeout(() => { toggleSel(pressLi); pressLi = null; }, 450);
  });
  const cancelPress = () => { clearTimeout(pressTimer); pressTimer = null; pressLi = null; };
  document.addEventListener("pointerup", cancelPress);
  document.addEventListener("pointercancel", cancelPress);
  document.addEventListener("pointermove", e => {
    if (pressTimer && (Math.abs(e.movementX) + Math.abs(e.movementY) > 6)) cancelPress();
  });
  async function relocateSelected(dest) {
    const items = [...selected.values()];
    if (!items.length || !dest) return;
    selBar.hidden = true;
    try {
      const j = await post("/relocate", {items, dest});
      toast(j.label, j.commit);
      if (j.warn) toast(j.warn);
      clearSel();
      await refresh();
    } catch (err) {
      paintSel();
      toast("Not saved: " + err.message);
    }
  }
  async function doneSelected() {
    const items = [...selected.values()];
    if (!items.length) return;
    selBar.hidden = true;
    try {
      const j = await post("/done-batch", {items});
      toast(j.label, j.commit);
      if (j.warn) toast(j.warn);
      clearSel();
      await refresh();
    } catch (err) {
      paintSel();
      toast("Not saved: " + err.message);
    }
  }
  async function urgencySelected(u) {
    const items = [...selected.values()];
    if (!items.length || !u) return;
    selBar.hidden = true;
    try {
      const j = await post("/urgency-batch", {items, urgency: u});
      toast(j.label, j.commit);
      if (j.warn) toast(j.warn);
      clearSel();
      await refresh();
    } catch (err) {
      paintSel();
      toast("Not saved: " + err.message);
    }
  }
  function pickUrgency(anchor) {
    menu(anchor, [["Any time", "anytime"], ["On the desk", "desk"], ["Urgent", "urgent"]].map(
      ([label, u]) => [label, () => urgencySelected(u)]));
  }
  function pickMove(anchor) {
    const domains = [...new Set(targets.map(t => t.domain).filter(Boolean))];
    if (!domains.length) {
      toast(targets.length ? "No domain labels on move targets" : "No move targets loaded");
      return;
    }
    menu(anchor, domains.map(dom => [dom, () => {
      const lists = targets.filter(t => t.domain === dom);
      menu(anchor, lists.map(t => {
        const label = t.kind === "project" ? t.title : `${dom} (general)`;
        return [label, () => relocateSelected(t.src)];
      }));
    }]));
  }
  function pickProject(anchor) {
    const dom = selectionDomain();
    if (!dom) {
      toast("Select tasks from one category");
      return;
    }
    const lists = targets.filter(t => t.domain === dom);
    if (!lists.length) {
      toast(`No ${dom} lists loaded`);
      return;
    }
    const items = lists.map(t => {
      const label = t.kind === "project" ? t.title : `${dom} (general)`;
      return [label, () => relocateSelected(t.src)];
    });
    items.push(["New project…", () => promptNewProject(anchor, dom)]);
    menu(anchor, items);
  }
  function promptNewProject(anchor, dom) {
    closeMenus();
    const wrap = document.createElement("div");
    wrap.className = "picker proj-new";
    wrap.innerHTML = '<label>New ' + esc(dom) + ' project'
      + '<input type="text" class="proj-name" placeholder="Name" autocomplete="off"></label>'
      + '<button type="button" class="ok">Create & move</button>'
      + '<button type="button" class="cancel">Cancel</button>';
    place(wrap, anchor);
    const inp = $(".proj-name", wrap);
    const go = async () => {
      const name = (inp.value || "").trim();
      if (!name) { inp.focus(); return; }
      wrap.remove();
      const items = [...selected.values()].map(({src, id}) => ({src, id}));
      if (!items.length) return;
      selBar.hidden = true;
      try {
        const j = await post("/relocate", {items, domain: dom, project: name, create: true});
        toast(j.label, j.commit);
        if (j.warn) toast(j.warn);
        clearSel();
        await refresh();
      } catch (err) {
        paintSel();
        toast("Not saved: " + err.message);
      }
    };
    $(".ok", wrap).onclick = e => { e.stopPropagation(); go(); };
    $(".cancel", wrap).onclick = e => { e.stopPropagation(); wrap.remove(); };
    inp.addEventListener("keydown", e => {
      if (e.key === "Enter") { e.preventDefault(); go(); }
      if (e.key === "Escape") { e.preventDefault(); wrap.remove(); }
    });
    inp.focus();
  }
  selBar.addEventListener("click", e => {
    const b = e.target.closest("button[data-s]");
    if (!b) return;
    // Keep the opening click from reaching the document closer (belt + suspenders).
    e.stopPropagation();
    if (b.dataset.s === "clear") { closeMenus(); return clearSel(); }
    if (b.dataset.s === "done") { closeMenus(); return doneSelected(); }
    if (b.dataset.s === "urgency") pickUrgency(b);
    if (b.dataset.s === "move") pickMove(b);
    if (b.dataset.s === "project") pickProject(b);
  });

  // ---- where a task came from
  async function showSource(li) {
    if (li.nextElementSibling?.classList.contains("srcrow")) return li.nextElementSibling.remove();
    const row = document.createElement("li");
    row.className = "blurb srcrow";
    row.innerHTML = '<span class="spin"></span>';
    li.after(row);
    try {
      const s = await (await fetch(`/source?src=${encodeURIComponent(li.dataset.src)}&id=${li.dataset.id}`)).json();
      if (s.error) throw new Error(s.error);
      const c = x => x ? `<a href="${esc(x.url)}">${esc(x.date)} ${esc(x.subject)}</a>` : "unknown";
      const notes = s.notes.map(n => `<a href="${esc(n.url)}">${esc(n.path)}</a>`).join(", ");
      row.innerHTML = `<span class="bt" dir="auto">Added: ${c(s.first)}${s.added ? " (➕ " + esc(s.added) + ")" : ""}<br>`
        + `Last changed: ${c(s.last)}<br>In <a href="${esc(s.file)}">${esc(li.dataset.src)}</a>`
        + (notes ? `<br>Mentioned in: ${notes}` : "") + `</span><span class="bb"><button>×</button></span>`;
    } catch (e) { row.dataset.state = "error"; row.innerHTML = `<span class="bt">${esc(e.message)}</span><span class="bb"><button>×</button></span>`; }
    $("button", row).onclick = () => row.remove();
  }

  // ---- weekly review
  document.addEventListener("click", async e => {
    const b = e.target.closest(".rv button");
    if (!b) return;
    e.stopPropagation();
    const li = b.closest("li.rvi"), r = b.dataset.r;
    if (r === "done" || r === "keep" || r === "drop") return act(li, r);
    if (r === "snooze") return menu(b, [["Tomorrow", () => act(li, "snooze", plus(1))],
      ["Next Sunday", () => act(li, "snooze", nextSunday())], ["+1 week", () => act(li, "snooze", plus(7))],
      ["+1 month", () => act(li, "snooze", plus(30))], ["Pick date…", () => pickDate(b, "", v => act(li, "snooze", v))]]);
    if (r === "answer" || r === "skip") {
      // Record the choice locally (no Claude). Vault edits for free-text review
      // items happen in the Cursor Project chat when needed.
      li.classList.add("working");
      try {
        await post("/review", {review: li.dataset.rid, answer: r === "skip" ? "skipped" : b.textContent});
        await refresh();
      } catch (err) { li.classList.remove("working"); toast("Not saved: " + err.message); }
    }
  });

  // ---- Keep-style add: Enter creates one task; multi-line / list paste splits
  // LIVE_JS is a plain string (not str.format); use single JS braces / \n escapes.
  const LISTY = /(?:\n|^)\s*(?:[-*+]\s+(?:\[[ xX-]\]\s+)?|\d+[.)]\s+)/m;
  async function submitAdd(row, blob) {
    if (!row || !String(blob || "").trim()) return;
    const inp = $(".add-inp", row);
    row.classList.add("working");
    if (inp) inp.disabled = true;
    try {
      const body = {
        text: blob,
        src: row.dataset.src || "",
        domain: row.dataset.domain || ($(".add-dom", row)?.value || ""),
        project: row.dataset.project || "",
        section: row.dataset.section || "Active",
        urgency: row.dataset.urgency || "",
      };
      const j = await post("/add", body);
      toast(j.label, j.commit);
      if (j.warn) toast(j.warn);
      if (inp) inp.value = "";
      await refresh();
    } catch (e) { toast("Not saved: " + e.message); }
    finally {
      row.classList.remove("working");
      if (inp) { inp.disabled = false; try { inp.focus(); } catch (_) {} }
    }
  }
  document.addEventListener("keydown", e => {
    const inp = e.target.closest?.(".add-inp");
    if (!inp || e.key !== "Enter" || e.isComposing) return;
    e.preventDefault();
    submitAdd(inp.closest(".add-row"), inp.value);
  });
  document.addEventListener("paste", e => {
    const inp = e.target.closest?.(".add-inp");
    if (!inp) return;
    const clip = e.clipboardData?.getData("text/plain") ?? "";
    if (!clip.includes("\n") && !LISTY.test(clip)) return;
    e.preventDefault();
    const start = inp.selectionStart ?? inp.value.length;
    const end = inp.selectionEnd ?? inp.value.length;
    submitAdd(inp.closest(".add-row"), inp.value.slice(0, start) + clip + inp.value.slice(end));
  });

  // ---- code TODO checkbox: strike through, wait, then commit the close to GitHub
  document.addEventListener("change", e => {
    const cb = e.target.closest(".ctl li > input.cdone");
    if (!cb) return;
    const li = cb.parentElement;
    if (!cb.checked) { clearTimeout(li._t); li.classList.remove("checked"); return; }
    li.classList.add("checked");
    li._t = setTimeout(async () => {
      cb.disabled = true;
      try {
        const j = await post("/code-todo/done", {repo: li.dataset.repo, path: li.dataset.path, key: li.dataset.key});
        li.classList.add("gone");
        setTimeout(() => li.remove(), 300);
        toast(j.label, j.commit, "/code-todo/undo");
      } catch (err) {
        cb.disabled = false; cb.checked = false; li.classList.remove("checked");
        toast("Not saved: " + err.message);
      }
    }, DONE_DELAY);
  });

  // ---- done checkbox: strike through, wait, then mark done
  document.addEventListener("change", e => {
    const cb = e.target.closest("li[data-id] > input.done");
    if (!cb) return;
    const li = cb.parentElement;
    if (!cb.checked) { clearTimeout(li._t); checking--; li.classList.remove("checked"); return; }
    li.classList.add("checked"); checking++;
    li._t = setTimeout(async () => {
      cb.disabled = true;
      try {
        const j = await post("/act", {src: li.dataset.src, id: li.dataset.id, op: "done"});
        li.classList.add("gone");
        setTimeout(() => { li.remove(); window.applyFilter(); }, 300);
        toast(j.label, j.commit);
        if (j.warn) toast(j.warn);
      } catch (err) {
        cb.disabled = false; cb.checked = false; li.classList.remove("checked");
        toast("Not saved: " + err.message);
      } finally { checking--; }
    }, DONE_DELAY);
  });

  // Pick up edits from other sessions when the tab comes back.
  document.addEventListener("visibilitychange", () => {
    if (!document.hidden && Date.now() - shownAt > 60000) refresh();
  });

  // ---- Pull vault + recollect (local-only; safe refresh without Claude)
  const pb = document.createElement("button");
  pb.className = "pullbtn"; pb.textContent = "↻ Pull";
  pb.title = "git pull the vault, then re-read tasks from disk";
  $(".top h1").after(pb);
  pb.onclick = () => pullRefresh();

  // ---- Update: pull vault, recollect, minimal stubs, re-render (no LLM)
  const ub = document.createElement("button");
  ub.className = "updbtn"; ub.textContent = "↻ Update";
  ub.title = "Refresh from disk/git: pull, recollect tasks, rebuild stubs (no Claude)";
  pb.after(ub);
  const panel = document.createElement("div");
  panel.className = "box info upd"; panel.hidden = true;
  $(".top").after(panel);
  function showUpd(j) {
    const fin = j.status === "done" || j.status === "error";
    ub.disabled = !!j.id && !fin;
    panel.hidden = !j.id;
    if (!j.id) return;
    panel.dataset.state = j.status;
    if (!fin) {
      panel.innerHTML = '<h2><span class="spin"></span>Refreshing from disk/git (pull, collect, stubs)</h2>';
      return;
    }
    if (j.status === "done" && !j.summary) { panel.hidden = true; return; }  // nothing to report
    panel.innerHTML = `<h2>Update ${j.status === "error" ? "failed" : "done"} <span class="n">${esc(j.finished || "")}</span></h2>`
      + '<div class="us"></div><button class="ux">Dismiss</button>';
    $(".us", panel).innerHTML = mdb(j.summary || "");
    $(".ux", panel).onclick = () => { panel.hidden = true; post("/dismiss", {job: j.id}).catch(() => {}); };
  }
  async function pollUpd() {
    for (;;) {
      await sleep(3000);
      const j = await (await fetch("/update")).json();
      if (j.status === "done" || j.status === "error") { location.reload(); return; }
    }
  }
  ub.onclick = async () => {
    try { const r = await post("/update", {}); showUpd({id: r.job, status: "running"}); pollUpd(); }
    catch (e) { toast("Can't update: " + e.message); }
  };
  // ---- Proposed by the last Update: confirm candidate tasks (＋ Add) and likely-done tasks (☑ Done)
  const prop = document.createElement("section");
  prop.className = "box info prop"; prop.hidden = true; prop.dataset.key = "prop";
  panel.after(prop);
  async function loadProps() {
    const p = await (await fetch("/proposals")).json();
    const li = (html, btns) => { const e = document.createElement("li"); e.innerHTML = html;
      for (const [label, fn] of btns) { const b = document.createElement("button"); b.textContent = label;
        b.onclick = async () => { b.disabled = true; try { await fn(e); e.remove(); tidy(); }
          catch (x) { toast(x.message); b.disabled = false; } };
        e.append(b); } return e; };
    const dismiss = key => () => post("/proposal/dismiss", {key});
    prop.innerHTML = "<h2>Proposed by Update</h2>";
    const add = (title, items) => { if (!items.length) return; const h = document.createElement("h3"); h.textContent = title;
      const ul = document.createElement("ul"); ul.append(...items); prop.append(h, ul); };
    add("Add as task?", p.candidates.map(c => li(
      `<span class="pt" dir="auto">${mdi(c.text)}${c.due ? ` <span class="why">📅 ${esc(c.due)}</span>` : ""}</span>`,
      [["＋ Add", async () => { const j = await post("/add", c); toast(j.label, j.commit); }],
       ["×", dismiss(c.text)]])));
    add("Done already?", p.likely_done.map(d => li(
      `<span class="pt" dir="auto">${mdi(d.text)} <span class="why">— ${mdi(d.why)}</span></span>`,
      [["☑ Done", async () => { const j = await post("/act", {src: d.src, id: d.id, op: "done", proposal: d.id}); toast(j.label, j.commit); }],
       ["×", dismiss(d.id)]])));
    tidy();
  }
  function tidy() {
    for (const ul of prop.querySelectorAll("ul")) if (!ul.children.length) { ul.previousElementSibling.remove(); ul.remove(); }
    prop.hidden = !prop.querySelector("li");
  }
  loadProps().catch(() => {});
  fetch("/update").then(r => r.json()).then(j => {
    showUpd(j);
    if (j.id && j.status !== "done" && j.status !== "error") pollUpd();
  }).catch(() => {});
})();
</script>"""


# ================================================================ notes
# Progress notes live in their own private git repo (the notes repo), NOT in the
# vault, so a coding machine (e.g. a remote compute server) can clone just the notes:
#   <notes>/<key>/<Note>.md   and   <notes>/<key>/attachments/<images>
# <key> = a vault task-list folder under tasks/ ("work/website", "work", "life", …): any
# folder with a TASKS.md has notes. Location: $ASSISTANT_NOTES, else <vault>/../notes.
# Never written to project code repos (some are public). Saves land on disk at once;
# git is batched by notes_sync(). See docs/decisions.md 2026-11-20.
NOTE_EXT = ".md"
NOTE_NAME_RE = re.compile(r"^[^/\\\x00-\x1f<>:\"|?*]{1,120}$")
IMAGE_TYPES = {"image/webp": ".webp", "image/png": ".png", "image/jpeg": ".jpg", "image/gif": ".gif"}
IMAGE_MAGIC = {".webp": (b"RIFF", 8, b"WEBP"), ".png": (b"\x89PNG\r\n\x1a\n", 0, b""),
               ".jpg": (b"\xff\xd8\xff", 0, b""), ".gif": (b"GIF8", 0, b"")}
MAX_UPLOAD = 15 * 1024 * 1024
FILE_TYPES = {".webp": "image/webp", ".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
              ".gif": "image/gif", ".svg": "image/svg+xml"}
NOTES_PULL_EVERY = 60
_notes_conflicts: list[dict] = []   # surfaced in the UI until dismissed
_notes_last_pull = 0.0


class NotesPathError(ValueError):
    pass


def notes_root(vault: Path) -> Path:
    return Path(os.environ.get("ASSISTANT_NOTES") or (Path(vault).parent / "notes")).expanduser()


def notes_repo(vault: Path) -> Path:
    root = notes_root(vault)
    if not (root / ".git").exists():
        raise LookupError(f"notes repo not found at {root} (clone your notes repo there "
                          "or set ASSISTANT_NOTES)")
    return root


def notes_projects(vault: Path) -> list[dict]:
    """Every vault tasks/ folder with a TASKS.md, with its notes from the notes repo."""
    tasks = vault / "tasks"
    root = notes_root(vault)
    out = []
    for tm in sorted(tasks.glob("**/TASKS.md")):
        d = tm.parent
        if d.is_symlink():
            continue
        key = d.relative_to(tasks).as_posix()
        cat = key.split("/")[0]
        head = next((l[2:].strip() for l in tm.read_text().splitlines() if l.startswith("# ")), key)
        nd = root / key
        notes = []
        if nd.is_dir() and not nd.is_symlink():
            for f in sorted(nd.glob(f"*{NOTE_EXT}"), key=lambda f: f.name.lower()):
                if f.is_file() and not f.is_symlink():
                    notes.append({"name": f.name[:-len(NOTE_EXT)], "mtime": int(f.stat().st_mtime),
                                  "conflict": ".conflict-" in f.name})
        out.append({"key": key, "title": head, "domain": CATEGORY_DIRS.get(cat, cat),
                    "kind": "category" if "/" not in key else "project", "notes": notes})
    return out


def notes_dir(vault: Path, key: str) -> Path:
    """The notes folder of a known vault task-list folder. Rejects anything else."""
    if not isinstance(key, str) or not re.fullmatch(r"[\w.-]+(?:/[\w.-]+)*", key) or \
            any(p in (".", "..") or p.startswith(".") for p in key.split("/")):
        raise NotesPathError("bad project key")
    if not (vault / "tasks" / key / "TASKS.md").is_file():
        raise NotesPathError("unknown project")
    cur = vault / "tasks"
    for part in key.split("/"):
        cur = cur / part
        if cur.is_symlink():
            raise NotesPathError("symlinked project folder")
    root = notes_root(vault)
    nd = root / key
    cur = root
    for part in key.split("/"):
        cur = cur / part
        if cur.is_symlink():
            raise NotesPathError("symlink in notes path")
    if nd.exists() and not nd.resolve().is_relative_to(root.resolve()):
        raise NotesPathError("notes folder outside the notes repo")
    return nd


def notes_path(vault: Path, key: str, rel: str) -> Path:
    """Resolve `rel` strictly inside the project's notes folder. Only two shapes exist:
    "<Note>.md" and "attachments/<file>" — no "..", absolute paths, other folders or symlinks."""
    nd = notes_dir(vault, key)
    if not isinstance(rel, str) or not rel or "\\" in rel or "\x00" in rel or re.match(r"^[A-Za-z]:", rel):
        raise NotesPathError("bad path")
    parts = rel.split("/")
    if any(p in ("", ".", "..") for p in parts) or len(parts) > 2 or (len(parts) == 2 and parts[0] != "attachments"):
        raise NotesPathError("bad path")
    cur = nd
    for part in parts:
        cur = cur / part
        if cur.is_symlink():
            raise NotesPathError("symlink in path")
    if (nd / "attachments").is_symlink():
        raise NotesPathError("symlinked attachments folder")
    if cur.exists() and not cur.resolve().is_relative_to(notes_root(vault).resolve()):
        raise NotesPathError("path escapes the notes repo")
    return cur


def note_file(vault: Path, key: str, name: str) -> Path:
    if not isinstance(name, str) or not NOTE_NAME_RE.match(name) or name.strip() != name \
            or name.startswith(".") or name.endswith("."):
        raise NotesPathError("bad note name")
    return notes_path(vault, key, name + NOTE_EXT)


def text_hash(b: bytes) -> str:
    return hashlib.sha1(b).hexdigest()


def note_read(vault: Path, key: str, name: str) -> dict:
    f = note_file(vault, key, name)
    if not f.is_file():
        raise LookupError("note not found")
    b = f.read_bytes()
    return {"key": key, "name": name, "text": b.decode("utf-8", errors="replace"), "hash": text_hash(b),
            "path": f.relative_to(notes_root(vault)).as_posix()}


def conflict_name(name: str, when: dt.datetime | None = None) -> str:
    return f"{name}.conflict-{(when or dt.datetime.now()).strftime('%Y%m%d-%H%M%S')}"


def note_save(vault: Path, key: str, name: str, text: str, base: str | None) -> dict:
    """Write the note now (commit comes later). If the file changed on disk since the
    browser loaded it (`base` = its hash then), keep the browser's text in the note and
    save the disk version beside it as <note>.conflict-<ts>.md — never lose either."""
    if not isinstance(text, str) or len(text) > 5_000_000:
        raise ValueError("note too large")
    notes_repo(vault)
    f = note_file(vault, key, name)
    data = text.encode("utf-8")
    conflict = None
    with _lock:
        if f.is_file():
            cur = f.read_bytes()
            if base and text_hash(cur) != base and cur != data:
                cname = conflict_name(name)
                note_file(vault, key, cname).write_bytes(cur)
                conflict = {"key": key, "note": name, "copy": cname, "why": "changed on disk while you edited",
                            "at": dt.datetime.now().isoformat(timespec="seconds")}
                _notes_conflicts.append(conflict)
        elif base:
            raise LookupError("note was renamed or deleted meanwhile; reload")
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_bytes(data)
    return {"hash": text_hash(data), "conflict": conflict, "dirty": True}


def note_create(vault: Path, key: str, name: str) -> dict:
    notes_repo(vault)
    f = note_file(vault, key, name)
    with _lock:
        if f.exists():
            raise LookupError("a note with that name exists")
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(f"# {name}\n\n")
    return note_read(vault, key, name)


def note_rename(vault: Path, key: str, name: str, new: str) -> dict:
    src, dst = note_file(vault, key, name), note_file(vault, key, new)
    with _lock:
        if not src.is_file():
            raise LookupError("note not found")
        if dst.exists() and dst.resolve() != src.resolve():
            raise LookupError("a note with that name exists")
        src.rename(dst)
    return {"name": new}


def sniff_image(data: bytes) -> str | None:
    for ext, (magic, off, extra) in IMAGE_MAGIC.items():
        if data.startswith(magic) and (not extra or data[off:off + len(extra)] == extra):
            return ext
    return None


def note_upload(vault: Path, key: str, ctype: str, data: bytes, now: dt.datetime | None = None) -> dict:
    """Save one pasted/dropped image as attachments/YYYY-MM-DD-HHMMSS.<ext>."""
    ext = IMAGE_TYPES.get((ctype or "").split(";")[0].strip().lower())
    if not ext:
        raise ValueError("only webp, png, jpeg or gif images")
    if not data or len(data) > MAX_UPLOAD:
        raise ValueError(f"image must be 1 byte to {MAX_UPLOAD // 2**20} MB")
    if sniff_image(data) != ext:
        raise ValueError("file content is not the declared image type")
    notes_repo(vault)
    stamp = (now or dt.datetime.now()).strftime("%Y-%m-%d-%H%M%S")
    with _lock:
        for n in range(100):
            rel = f"attachments/{stamp}{'' if not n else f'-{n}'}{ext}"
            f = notes_path(vault, key, rel)
            if not f.exists():
                break
        else:
            raise LookupError("too many uploads this second")
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_bytes(data)
    return {"path": rel}


def note_asset(vault: Path, key: str, rel: str) -> tuple[bytes, str]:
    f = notes_path(vault, key, rel)
    ctype = FILE_TYPES.get(f.suffix.lower())
    if not ctype or not f.is_file():
        raise LookupError("not found")
    return f.read_bytes(), ctype


def dirty_notes(vault: Path) -> list[str]:
    root = notes_root(vault)
    if not (root / ".git").exists():
        return []
    out = git(root, "status", "--porcelain", "-z", "--untracked-files=all").stdout
    return [e[3:] for e in filter(None, out.split("\0")) if len(e) > 3]


def notes_commit_msg(paths: list[str]) -> str:
    notes = sorted({p[:-len(NOTE_EXT)] for p in paths if p.endswith(NOTE_EXT)})
    if not notes:
        keys = sorted({re.sub(r"/attachments/.*$", "", p) for p in paths})
        return f"notes: {keys[0]} attachments" + (f" (+{len(keys) - 1} more)" if len(keys) > 1 else "")
    return f"notes: {notes[0]}" + (f" (+{len(notes) - 1} more)" if len(notes) > 1 else "")


def _rebase_in_progress(repo: Path) -> bool:
    gd = git(repo, "rev-parse", "--git-dir").stdout.strip()
    g = Path(gd) if Path(gd).is_absolute() else repo / gd
    return (g / "rebase-merge").exists() or (g / "rebase-apply").exists()


def resolve_pull_conflict(repo: Path) -> list[dict]:
    """A `pull --rebase` of the notes repo stopped on conflicts. Abort it, merge the
    remote with our side winning (-X ours), and save each conflicting note's remote
    version as <note>.conflict-<ts>.md. Same policy as .notes/sync.sh in the repo."""
    unmerged = [l for l in git(repo, "diff", "--name-only", "--diff-filter=U").stdout.splitlines() if l]
    if _rebase_in_progress(repo):
        git(repo, "rebase", "--abort")
    if not unmerged:
        return []
    m = git(repo, "merge", "-q", "--no-edit", "-X", "ours", "@{u}")
    if m.returncode != 0:
        git(repo, "merge", "--abort")
        return []
    when, found, copies = dt.datetime.now(), [], []
    for u in unmerged:
        remote = git(repo, "show", f"@{{u}}:{u}")
        if remote.returncode != 0 or not u.endswith(NOTE_EXT):
            continue
        src = Path(u)
        name = src.name[:-len(NOTE_EXT)]
        copy = src.with_name(conflict_name(name, when) + NOTE_EXT)
        (repo / copy).write_text(remote.stdout)
        copies.append(copy.as_posix())
        found.append({"key": src.parent.as_posix(), "note": name, "copy": copy.name[:-len(NOTE_EXT)],
                      "why": "changed elsewhere (another machine or chat) at the same time",
                      "at": when.isoformat(timespec="seconds")})
    if copies:
        git(repo, "add", "--", *copies)
        git(repo, "commit", "-q", "-m", f"notes: conflict copy {copies[0]}", "--", *copies)
    _notes_conflicts.extend(found)
    return found


def notes_pull(repo: Path) -> None:
    """Pull the notes repo; callers hold _lock and have committed local edits."""
    global _notes_last_pull
    r = git(repo, "pull", "-q", "--rebase")
    if r.returncode != 0 and _rebase_in_progress(repo):
        resolve_pull_conflict(repo)
    _notes_last_pull = time.time()


def notes_refresh(vault: Path) -> None:
    """Opportunistic pull (≤ once a minute) so edits from other machines show up.
    Skipped while there are unsynced local edits: the next notes_sync handles those."""
    root = notes_root(vault)
    if not (root / ".git").exists() or time.time() - _notes_last_pull < NOTES_PULL_EVERY:
        return
    if _lock.acquire(blocking=False):
        try:
            if not dirty_notes(vault):
                notes_pull(root)
        finally:
            _lock.release()


def notes_index(vault: Path) -> str:
    """PROJECTS.md for the notes repo: folder ↔ title ↔ code repos (from memory/projects)."""
    projects = {p["slug"]: p for p in (parse_project(vault, f) for f in sorted((vault / "memory/projects").glob("*.md"))
                                       if f.name != "README.md")}
    rows = []
    for p in notes_projects(vault):
        slug = p["key"].split("/")[-1]
        meta = projects.get(slug, {}) if p["kind"] == "project" else {}
        repos = " ".join(f"`{r}`" for r in meta.get("repos", []) + meta.get("related", []) if REPO_RE.match(r))
        status = meta.get("status") or ""
        rows.append(f"| `{p['key']}/` | {p['title']} | {repos or '—'} | {status} |")
    return ("# Projects\n\nGenerated by the assistant dashboard from the vault. Folder = where this "
            "project's notes live; Code = GitHub `owner/repo[@branch]` of its code.\n\n"
            "| Folder | Project | Code | Status |\n|---|---|---|---|\n" + "\n".join(rows) + "\n")


def notes_sync(vault: Path) -> dict:
    """Commit every local note change in one commit, refresh PROJECTS.md, pull --rebase
    (conflicts keep both versions), push. Called on Save, ~60 s idle, note switch, tab leave."""
    repo = notes_repo(vault)
    with _lock:
        before = len(_notes_conflicts)
        idx = repo / "PROJECTS.md"
        text = notes_index(vault)
        if not idx.is_file() or idx.read_text() != text:
            idx.write_text(text)
        pending = dirty_notes(vault)
        sha = None
        if pending:
            git(repo, "add", "-A")
            c = git(repo, "commit", "-q", "-m", notes_commit_msg([p for p in pending if p != "PROJECTS.md"] or pending))
            sha = git(repo, "rev-parse", "HEAD").stdout.strip() if c.returncode == 0 else None
        notes_pull(repo)
        warn = ""
        if sha or pending or git(repo, "status", "-sb").stdout.split("\n")[0].find("ahead") >= 0:
            p = git(repo, "push", "-q")
            if p.returncode != 0:
                notes_pull(repo)
                p = git(repo, "push", "-q")
                warn = "" if p.returncode == 0 else "committed, push failed: " + p.stderr.strip()[:150]
        return {"ok": True, "committed": bool(sha), "warn": warn,
                "conflicts": _notes_conflicts[before:], "dirty": bool(dirty_notes(vault))}


def notes_tasks(vault: Path, key: str) -> list[dict]:
    notes_dir(vault, key)   # validates key
    src = f"tasks/{key}/TASKS.md"
    return [{"id": t.get("id"), "text": t["text"], "due": t["due"], "section": t["section"]}
            for t in parse_tasks_md(vault / src, src) if t["section"] != "Done"]


# ---------------------------------------------------------------- notes: Obsidian import
WIKI_EMBED_RE = re.compile(r"!\[\[([^\]|#]+?)(?:#[^\]|]*)?(?:\|([^\]]*))?\]\]")
WIKI_LINK_RE = re.compile(r"(?<!!)\[\[([^\]|#]+?)(#[^\]|]*)?(?:\|([^\]]*))?\]\]")
MD_IMAGE_RE = re.compile(r"!\[([^\]]*)\]\((<[^>]+>|[^)\s]+)\)")


def md_url(path: str) -> str:
    return quote(path, safe="/#-._~")


def convert_obsidian(text: str, attach, link) -> str:
    """Obsidian → standard markdown. `attach(name)` copies an embedded file and returns
    its new relative path ("attachments/x.png") or None; `link(note_name)` returns the
    relative .md path of an imported note or None. Unresolved syntax is left as is."""
    def emb(m):
        target, size = m.group(1).strip(), m.group(2)
        if Path(target).suffix.lower() in FILE_TYPES:
            rel = attach(target)
            return f"![]({md_url(rel)})" if rel else m.group(0)
        rel = link(target)                       # ![[Other note]] embeds a note: link it
        return f"[{Path(target).stem}]({md_url(rel)})" if rel else m.group(0)

    def wl(m):
        target, head, alias = m.group(1).strip(), m.group(2) or "", m.group(3)
        rel = link(target)
        if not rel:
            return m.group(0)
        label = alias or (target + (f" › {head[1:]}" if head else ""))
        anchor = ("#" + re.sub(r"[^\w\- ]", "", head[1:]).strip().lower().replace(" ", "-")) if head else ""
        return f"[{label}]({md_url(rel)}{anchor})"

    def mdimg(m):
        raw = m.group(2).strip("<>")
        if re.match(r"^[a-z]+:", raw, re.I) or raw.startswith("attachments/"):
            return m.group(0)
        from urllib.parse import unquote
        rel = attach(unquote(raw))
        return f"![{m.group(1)}]({md_url(rel)})" if rel else m.group(0)

    # Leave fenced code untouched.
    parts = re.split(r"(```[\s\S]*?```|~~~[\s\S]*?~~~)", text)
    for i in range(0, len(parts), 2):
        parts[i] = MD_IMAGE_RE.sub(mdimg, WIKI_LINK_RE.sub(wl, WIKI_EMBED_RE.sub(emb, parts[i])))
    return "".join(parts)


def safe_attachment_name(name: str) -> str:
    stem, ext = os.path.splitext(Path(name).name)
    stem = re.sub(r"[^\w.\-]+", "-", stem, flags=re.UNICODE).strip("-.") or "image"
    return stem + ext.lower()


def import_obsidian(src: Path, vault: Path, mapping: dict, dry_run: bool = False, commit: bool = True) -> dict:
    """Copy mapped Obsidian notes (+ the files they embed) into tasks/<key>/notes/.
    mapping: {"relative/Note.md": "work/website" | null (skip)}. The source is only read."""
    files = [f for f in src.rglob("*") if f.is_file()
             and not any(part in (".obsidian", ".trash") for part in f.relative_to(src).parts)]
    by_name: dict[str, list[Path]] = {}
    for f in files:
        by_name.setdefault(f.name.lower(), []).append(f)
    plan = {rel: key for rel, key in mapping.items() if key}
    notes_by_name = {Path(rel).stem.lower(): (rel, key) for rel, key in plan.items()}
    report, per_key = {"notes": [], "attachments": [], "unresolved": []}, {}
    for rel, key in plan.items():
        srcf = src / rel
        if not srcf.is_file():
            raise LookupError(f"not in the Obsidian vault: {rel}")
        nd = notes_dir(vault, key)
        name = Path(rel).stem
        copied: dict[Path, str] = {}

        def attach(target, nd=nd, srcf=srcf, copied=copied):
            cand = [(srcf.parent / target)] if "/" in target else []
            cand += [src / target] + by_name.get(Path(target).name.lower(), [])
            f = next((c for c in cand if c.is_file()), None)
            if not f:
                report["unresolved"].append(f"{rel}: {target}")
                return None
            if f in copied:
                return copied[f]
            base = safe_attachment_name(f.name)
            dest, n = nd / "attachments" / base, 1
            while (dest.exists() and dest.read_bytes() != f.read_bytes()) or \
                    any(v == f"attachments/{dest.name}" for k, v in copied.items() if k != f):
                stem, ext = os.path.splitext(base)
                dest, n = nd / "attachments" / f"{stem}-{n}{ext}", n + 1
            if not dry_run:
                dest.parent.mkdir(parents=True, exist_ok=True)
                if not dest.exists():
                    shutil.copyfile(f, dest)
            copied[f] = f"attachments/{dest.name}"
            report["attachments"].append(f"{key}/{copied[f]}")
            return copied[f]

        def link(target, nd=nd):
            hit = notes_by_name.get(Path(target).stem.lower())
            if not hit:
                return None
            other = notes_dir(vault, hit[1]) / (Path(hit[0]).stem + NOTE_EXT)
            return os.path.relpath(other, nd).replace(os.sep, "/")

        out = convert_obsidian(srcf.read_text(encoding="utf-8"), attach, link)
        dest = note_file(vault, key, name)
        if dest.exists() and dest.read_text() != out:
            raise LookupError(f"would overwrite an existing note: {key}/{dest.name}")
        if not dry_run:
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_text(out)
        report["notes"].append(f"{rel} → {key}/{dest.name}")
        per_key.setdefault(key, []).append(dest)
    report["commits"] = []
    if commit and not dry_run:
        repo = notes_repo(vault)
        for key, dests in per_key.items():
            git(repo, "add", "-A", "--", key)
            c = git(repo, "commit", "-q", "-m", f"notes: import {len(dests)} from Obsidian into {key}", "--", key)
            if c.returncode == 0:
                report["commits"].append(git(repo, "rev-parse", "--short", "HEAD").stdout.strip())
        if report["commits"]:
            p = git(repo, "push", "-q")
            report["push"] = "pushed" if p.returncode == 0 else "push failed: " + p.stderr.strip()[:150]
    return report


# ---------------------------------------------------------------- notes: link into code folders
def config_dir() -> Path:
    return Path(os.environ.get("ASSISTANT_CONFIG_DIR") or Path.home() / ".config" / "assistant")


def resolve_notes_key(vault: Path, target: str) -> str:
    if "/" in target:
        notes_dir(vault, target)
        return target
    hits = sorted(p.parent.relative_to(vault / "tasks").as_posix()
                  for p in (vault / "tasks").glob(f"*/{target}/TASKS.md"))
    if (vault / "tasks" / target / "TASKS.md").is_file():
        hits.insert(0, target)
    if len(hits) != 1:
        raise LookupError(f"project '{target}': {'not found' if not hits else 'ambiguous: ' + ', '.join(hits)}")
    return hits[0]


def read_links(cfg: Path) -> dict:
    import tomllib
    f = cfg / "notes-links.toml"
    return tomllib.loads(f.read_text()).get("links", {}) if f.is_file() else {}


def write_links(cfg: Path, links: dict) -> None:
    cfg.mkdir(parents=True, exist_ok=True)
    lines = ["# Machine-specific: project key -> code folder that has a progress/ link to its vault notes.",
             "# Written by `dashboard.py link-notes`. Not in git.", "", "[links]"]
    lines += [f"{json.dumps(k)} = {json.dumps(v)}" for k, v in sorted(links.items())]
    (cfg / "notes-links.toml").write_text("\n".join(lines) + "\n")


def exclude_path(repo: Path) -> Path:
    r = subprocess.run(["git", "-C", str(repo), "rev-parse", "--git-path", "info/exclude"],
                       capture_output=True, text=True)
    if r.returncode != 0:
        raise LookupError(f"not a git repository: {repo}")
    p = Path(r.stdout.strip())
    return p if p.is_absolute() else repo / p


def add_exclude(repo: Path, entry: str = "/progress") -> bool:
    """Hide `entry` from git via .git/info/exclude (local only, never committed)."""
    ex = exclude_path(repo)
    ex.parent.mkdir(parents=True, exist_ok=True)
    lines = ex.read_text().splitlines() if ex.is_file() else []
    if entry in lines or entry.lstrip("/") in lines:
        return False
    with ex.open("a") as f:
        if lines and lines[-1].strip():
            f.write("\n")
        f.write(f"# notes from the private assistant vault (dashboard.py link-notes)\n{entry}\n")
    return True


def git_sees(repo: Path, name: str = "progress") -> str:
    return subprocess.run(["git", "-C", str(repo), "status", "--porcelain", "--untracked-files=all", "--", name],
                          capture_output=True, text=True).stdout.strip()


def link_notes(vault: Path, target: str, code: Path, cfg: Path | None = None) -> dict:
    """Make the project's vault notes visible inside its code folder, invisible to git.
    WSL clone: a `progress/` symlink + .git/info/exclude. Windows-native clone (/mnt/<drive>/…):
    a .code-workspace file (outside the repo) that opens code + notes together."""
    cfg = cfg or config_dir()
    key = resolve_notes_key(vault, target)
    notes_repo(vault)
    nd = notes_dir(vault, key)
    nd.mkdir(parents=True, exist_ok=True)
    code = code.expanduser().resolve()
    exclude_path(code)  # must be a git repo
    res = {"key": key, "code": str(code), "notes": str(nd)}
    if re.match(r"^/mnt/[a-z]/", str(code)):
        def winpath(p):
            r = subprocess.run(["wslpath", "-w", str(p)], capture_output=True, text=True)
            return r.stdout.strip() if r.returncode == 0 else str(p)
        ws = cfg / "workspaces" / f"{key.replace('/', '-')}.code-workspace"
        ws.parent.mkdir(parents=True, exist_ok=True)
        ws.write_text(json.dumps({"folders": [{"name": code.name, "path": winpath(code)},
                                              {"name": "progress (vault notes)", "path": winpath(nd)}]}, indent=2) + "\n")
        res.update(method="workspace", workspace=str(ws))
    else:
        link = code / "progress"
        if link.is_symlink():
            if link.resolve() != nd.resolve():
                raise LookupError(f"{link} already links elsewhere ({os.readlink(link)})")
        elif link.exists():
            raise LookupError(f"{link} exists and is not our link; move it first")
        else:
            link.symlink_to(nd, target_is_directory=True)
        res["excluded"] = add_exclude(code)
        res["git_sees"] = git_sees(code)
        if res["git_sees"]:
            raise LookupError(f"git still sees progress/: {res['git_sees']}")
        res["method"] = "symlink"
    links = read_links(cfg)
    links[key] = str(code)
    write_links(cfg, links)
    return res


# ---------------------------------------------------------------- serve

SRC_RE = re.compile(
    rf"^(TASKS\.md|tasks/{CAT_FOLDER_RE}(?:/[\w-]+)?/TASKS\.md|memory/projects/[\w-]+\.md|inbox/[\w.-]+\.md)$")
# File headings may still include Waiting On / Someday; UI no longer parks there.
SECTIONS = ("Active", "Waiting On", "Someday")
WRITE_SECTIONS = ("Active",)
PULL_EVERY = 60
AGENT_MODEL = "claude-sonnet-5-5"
AGENT_TIMEOUT = 180
JOB_TTL = 2 * 3600
AGENT_PROMPT = """You are editing the user's assistant vault (the current directory). Follow the task
conventions in CLAUDE.md. {about}

the user's instruction: {text}

Edit vault files only. Do not run git; the caller commits and pushes. If the
instruction is ambiguous, change nothing and reply with "QUESTION: " and one short
question. Otherwise end with one short sentence to the user saying what you changed.
Put any command or text the user should copy in a fenced code block (```bash for commands)."""

_lock = threading.Lock()  # one writer to the vault at a time (actions and agent jobs)
_last_pull = 0.0
_ours: set[str] = set()   # commits made by this server; only these can be undone
_jobs: dict[str, dict] = {}
_queue: queue.Queue = queue.Queue()


def git(vault: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", "-C", str(vault), *args], capture_output=True, text=True, timeout=60)


def pull(vault: Path, force: bool = False) -> None:
    """Callers hold _lock; GETs pull only when the lock is free."""
    global _last_pull
    if force or time.time() - _last_pull > PULL_EVERY:
        r = git(vault, "pull", "-q", "--rebase", "--autostash")
        if r.returncode != 0 and _rebase_in_progress(vault):
            git(vault, "rebase", "--abort")
        _last_pull = time.time()


def push(vault: Path) -> str:
    if git(vault, "push", "-q").returncode == 0:
        return ""
    pull(vault, force=True)
    p = git(vault, "push", "-q")
    return "" if p.returncode == 0 else "committed, push failed: " + p.stderr.strip()[:150]


def commit_push(vault: Path, paths: list[str], msg: str) -> tuple[str | None, str]:
    git(vault, "add", "-A", "--", *paths)
    c = git(vault, "commit", "-q", "-m", msg, "--", *paths)
    if c.returncode != 0:
        return None, "saved, commit failed: " + (c.stderr or c.stdout).strip()[:150]
    warn = push(vault)
    sha = git(vault, "rev-parse", "HEAD").stdout.strip()  # after push: a rebase may have moved it
    _ours.add(sha)
    return sha, warn


def indent(line: str) -> int:
    return len(line) - len(line.lstrip())


def find_block(lines: list[str], tid: str) -> tuple[int, int]:
    """Line range of the open task whose raw text hashes to tid, with its continuation lines."""
    i = next((k for k, l in enumerate(lines) if (m := TASK_RE.match(l)) and task_id(m.group(1)) == tid), None)
    if i is None:
        raise LookupError("task not found (changed since the page loaded); reload")
    j = i + 1
    while j < len(lines) and lines[j].strip() and indent(lines[j]) > indent(lines[i]):
        j += 1
    return i, j


def move_block(lines: list[str], i: int, j: int, section: str) -> bool:
    """Move lines[i:j] to the end of `## section`. False if the file has no such section."""
    h = next((k for k, l in enumerate(lines) if l.strip() == f"## {section}"), None)
    if h is None or i <= h < j:
        return False
    block = lines[i:j]
    del lines[i:j]
    if h > i:
        h -= len(block)
    k = h + 1
    while k < len(lines) and not lines[k].startswith("## "):
        k += 1
    while k > h + 1 and not lines[k - 1].strip():
        k -= 1
    lines[k:k] = block
    return True


def set_date(line: str, emoji: str, value: str) -> str:
    line = re.sub(rf"\s*{emoji}\s*{DATE}", "", line).rstrip()
    return f"{line} {emoji} {value}" if value else line


def act(vault: Path, src: str, tid: str, op: str, arg: str = "") -> dict:
    """Deterministic edits: done, drop, keep, due, snooze, urgency, move, relocate.
    One commit each (relocate may touch two paths). Priority (P1–P3) is removed."""
    if op == "relocate":
        return relocate_many(vault, [{"src": src, "id": tid}], arg)
    if not SRC_RE.match(src):
        raise ValueError(f"bad source {src!r}")
    if op in ("due", "snooze") and arg and not re.fullmatch(DATE, arg):
        raise ValueError(f"bad date {arg!r}")
    if op == "urgency" and arg not in URGENCY_BUCKETS:
        raise ValueError(f"bad urgency {arg!r}")
    if op == "move" and (not src.endswith("TASKS.md") or arg not in WRITE_SECTIONS):
        raise ValueError(f"can't move to {arg!r}")
    if op == "prio":
        raise ValueError("priority removed; use urgency (⇢) instead")
    if op not in ("done", "drop", "keep", "due", "snooze", "urgency", "move"):
        raise ValueError(f"unknown action {op!r}")
    with _lock:
        pull(vault, force=True)
        path = vault / src
        lines = path.read_text().split("\n")
        i, j = find_block(lines, tid)
        text = parse_task(TASK_RE.match(lines[i]).group(1), src)["text"]
        if op == "done":
            lines[i] = lines[i].replace("- [ ]", "- [x]", 1).rstrip() + f" ✅ {dt.date.today().isoformat()}"
            move_block(lines, i, j, "Done")
            label = "Done"
        elif op == "drop":
            lines[i] = lines[i].replace("- [ ]", "- [-]", 1).rstrip() + f" ❌ {dt.date.today().isoformat()}"
            move_block(lines, i, j, "Done")
            label = "Dropped"
        elif op == "keep":
            lines[i] = set_date(lines[i], "👀", dt.date.today().isoformat())
            label = "Still open"
        elif op == "due":
            lines[i] = set_date(lines[i], "📅", arg)
            label = f"Due {arg}" if arg else "Due date cleared"
        elif op == "snooze":
            lines[i] = set_date(lines[i], "⏳", arg)
            label = f"Snoozed to {arg}" if arg else "Unsnoozed"
        elif op == "urgency":
            lines[i] = apply_urgency(lines[i], arg)
            move_block(lines, i, j, "Active")  # no-op when already under Active
            label = URGENCY_LABELS[arg]
        else:
            move_block(lines, i, j, arg)
            label = f"Moved to {arg}"
        path.write_text("\n".join(lines))
        sha, warn = commit_push(vault, [src], f"{op}{' ' + arg if arg else ''}: {text[:70]}")
        return {"ok": True, "text": text, "label": f"{label}: {text[:60]}", "commit": sha, "warn": warn}


def _clean_src_id_items(items: list[dict]) -> list[tuple[str, str]]:
    cleaned: list[tuple[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for it in items:
        src, tid = str(it.get("src", "")), str(it.get("id", ""))
        if not SRC_RE.match(src) or not tid or (src, tid) in seen:
            continue
        seen.add((src, tid))
        cleaned.append((src, tid))
    return cleaned


def resolve_relocate_dest(vault: Path, dest: str = "", domain: str = "",
                         project: str = "", create: bool = False) -> str:
    """Resolve relocate destination path; optionally create a nested project list."""
    dest = (dest or "").strip()
    dom = normalize_domain(domain) if domain else None
    proj_raw = (project or "").strip()
    if dest and not proj_raw and not create:
        if not SRC_RE.match(dest) or not dest.endswith("TASKS.md"):
            raise ValueError(f"bad destination {dest!r}")
        return dest
    if proj_raw or create:
        if not dom:
            raise ValueError("domain required to create or target a project")
        if not proj_raw:
            raise ValueError("project name required")
        # Accept either a display name (slugified) or an existing slug.
        slug = proj_raw if re.fullmatch(r"[\w-]+", proj_raw) else project_slug(proj_raw)
        title = proj_raw if proj_raw != slug else slug.replace("-", " ")
        if create:
            return ensure_project_tasks_md(vault, dom, slug, title=title)
        cat = category_folder(dom)
        if not cat:
            raise ValueError(f"bad domain {domain!r}")
        rel = f"tasks/{cat}/{slug}/TASKS.md"
        if not (vault / rel).is_file():
            raise ValueError(f"missing {rel}")
        return rel
    if dest:
        if not SRC_RE.match(dest) or not dest.endswith("TASKS.md"):
            raise ValueError(f"bad destination {dest!r}")
        return dest
    if dom:
        cat_src = _category_tasks_src(vault, dom)
        if not cat_src:
            raise ValueError(f"no category TASKS.md for {dom}")
        return cat_src
    raise ValueError("destination required")


def relocate_many(vault: Path, items: list[dict], dest: str = "", *,
                  domain: str = "", project: str = "", create: bool = False) -> dict:
    """Move one or more open task blocks into dest TASKS.md ## Active. One commit.

    `create=True` with domain + project name/slug makes tasks/<cat>/<slug>/TASKS.md
    when missing (Project… → New project…).
    """
    cleaned = _clean_src_id_items(items)
    if not cleaned:
        raise ValueError("no tasks to move")
    with _lock:
        pull(vault, force=True)
        dest = resolve_relocate_dest(vault, dest=dest, domain=domain,
                                    project=project, create=create)
        if not SRC_RE.match(dest) or not dest.endswith("TASKS.md"):
            raise ValueError(f"bad destination {dest!r}")
        if not (vault / dest).is_file() and dest != "TASKS.md":
            raise ValueError(f"missing {dest}")
        _, tag_dom = resolve_add_src(vault, src=dest)
        by_src: dict[str, list[str]] = {}
        texts: list[str] = []
        for src, tid in cleaned:
            lines = by_src.get(src)
            if lines is None:
                lines = (vault / src).read_text().split("\n")
                by_src[src] = lines
            i, j = find_block(lines, tid)
            block = lines[i:j]
            del lines[i:j]
            block[0] = retag_for_dest(block[0], dest, tag_dom)
            texts.append(parse_task(TASK_RE.match(block[0]).group(1), dest)["text"])
            if src == dest:
                _insert_lines_in_section(lines, "Active", block)
            else:
                by_src.setdefault(dest, (vault / dest).read_text().split("\n")
                                  if (vault / dest).is_file()
                                  else [f"# {dest}", "", "## Active", ""])
                _insert_lines_in_section(by_src[dest], "Active", block)
        paths = []
        for rel, lines in by_src.items():
            (vault / rel).parent.mkdir(parents=True, exist_ok=True)
            (vault / rel).write_text("\n".join(lines))
            paths.append(rel)
        n = len(texts)
        msg = f"relocate → {dest}: {texts[0][:70]}" if n == 1 else f"relocate → {dest}: {n} tasks"
        sha, warn = commit_push(vault, paths, msg)
        label = f"Moved to {dest}: {texts[0][:50]}" if n == 1 else f"Moved {n} tasks → {dest}"
        return {"ok": True, "text": texts[0], "texts": texts, "n": n, "dest": dest,
                "label": label, "commit": sha, "warn": warn}


def done_many(vault: Path, items: list[dict]) -> dict:
    """Mark one or more open tasks done (checkbox + ✅ + ## Done). One commit."""
    cleaned = _clean_src_id_items(items)
    if not cleaned:
        raise ValueError("no tasks to mark done")
    today = dt.date.today().isoformat()
    with _lock:
        pull(vault, force=True)
        by_src: dict[str, list[str]] = {}
        texts: list[str] = []
        for src, tid in cleaned:
            lines = by_src.get(src)
            if lines is None:
                lines = (vault / src).read_text().split("\n")
                by_src[src] = lines
            i, j = find_block(lines, tid)
            text = parse_task(TASK_RE.match(lines[i]).group(1), src)["text"]
            lines[i] = lines[i].replace("- [ ]", "- [x]", 1).rstrip() + f" ✅ {today}"
            move_block(lines, i, j, "Done")
            texts.append(text)
        paths = []
        for rel, lines in by_src.items():
            (vault / rel).write_text("\n".join(lines))
            paths.append(rel)
        n = len(texts)
        msg = f"done: {texts[0][:70]}" if n == 1 else f"done: {n} tasks"
        sha, warn = commit_push(vault, paths, msg)
        label = f"Done: {texts[0][:60]}" if n == 1 else f"Done: {n} tasks"
        return {"ok": True, "text": texts[0], "texts": texts, "n": n,
                "label": label, "commit": sha, "warn": warn}


def urgency_many(vault: Path, items: list[dict], bucket_name: str) -> dict:
    """Batch-set Today urgency (📅 / clear ⏳) for selected open tasks. One commit."""
    if bucket_name not in URGENCY_BUCKETS:
        raise ValueError(f"bad urgency {bucket_name!r}")
    cleaned = _clean_src_id_items(items)
    if not cleaned:
        raise ValueError("no tasks to set urgency")
    ulabel = URGENCY_LABELS[bucket_name]
    with _lock:
        pull(vault, force=True)
        by_src: dict[str, list[str]] = {}
        texts: list[str] = []
        for src, tid in cleaned:
            lines = by_src.get(src)
            if lines is None:
                lines = (vault / src).read_text().split("\n")
                by_src[src] = lines
            i, j = find_block(lines, tid)
            text = parse_task(TASK_RE.match(lines[i]).group(1), src)["text"]
            lines[i] = apply_urgency(lines[i], bucket_name)
            move_block(lines, i, j, "Active")  # no-op when already under Active
            texts.append(text)
        paths = []
        for rel, lines in by_src.items():
            (vault / rel).write_text("\n".join(lines))
            paths.append(rel)
        n = len(texts)
        msg = (f"urgency {bucket_name}: {texts[0][:70]}" if n == 1
               else f"urgency {bucket_name}: {n} tasks")
        sha, warn = commit_push(vault, paths, msg)
        label = (f"{ulabel}: {texts[0][:60]}" if n == 1
                 else f"{ulabel}: {n} tasks")
        return {"ok": True, "text": texts[0], "texts": texts, "n": n,
                "urgency": bucket_name, "label": label, "commit": sha, "warn": warn}


def undo(vault: Path, sha: str) -> dict:
    if sha not in _ours:
        raise ValueError("not a commit made by this dashboard")
    with _lock:
        pull(vault, force=True)
        r = git(vault, "revert", "--no-edit", sha)
        if r.returncode != 0:
            git(vault, "revert", "--abort")
            raise LookupError("can't undo automatically: later changes touch the same lines")
        _ours.discard(sha)
        return {"ok": True, "warn": push(vault)}


def dirty(vault: Path) -> dict[str, str]:
    """Uncommitted paths -> content hash."""
    out = git(vault, "status", "--porcelain", "-z", "--untracked-files=all").stdout
    res = {}
    for entry in filter(None, out.split("\0")):
        if len(entry) > 3 and entry[2] == " ":
            path = entry[3:]
            f = vault / path
            res[path] = hashlib.sha1(f.read_bytes()).hexdigest() if f.is_file() else "-"
    return res


def open_tasks(vault: Path) -> list[dict]:
    return [parse_task(m.group(1), str(f.relative_to(vault)))
            for f in task_list_files(vault) if f.is_file()
            for line in f.read_text().splitlines() if (m := TASK_RE.match(line))]


def find_anchor(vault: Path, tid: str, text: str) -> str | None:
    """The task's id after an agent edit: same id, else the most similar open task."""
    tasks = open_tasks(vault)
    if any(t["id"] == tid for t in tasks):
        return tid
    best = max(tasks, key=lambda t: difflib.SequenceMatcher(None, t["text"], text).ratio(), default=None)
    return best["id"] if best and difflib.SequenceMatcher(None, best["text"], text).ratio() >= 0.6 else None


def run_job(vault: Path, job: dict) -> None:
    job["status"] = "running"
    bin_path = claude_bin()
    if not bin_path:
        job.update(status="error",
                   summary="Claude CLI not installed; use deterministic controls "
                           "(done / due / snooze / move) or ask in the Cursor Project.",
                   anchor=job.get("tid"), commit=None, question=False)
        return
    cmd = [bin_path, "-p", "--output-format", "json",
           "--model", AGENT_MODEL, "--tools", "Read,Edit,Write,Glob,Grep", "--permission-mode", "acceptEdits",
           "--strict-mcp-config"]
    with _lock:
        pull(vault, force=True)
        if job["session"]:
            cmd += ["--resume", job["session"], job["text"]]
        else:
            about = ""
            if job.get("review_q"):
                about = (f"Weekly review question (about `{job['src']}`): {job['review_q']}"
                         f"{' (' + job['why'] + ')' if job.get('why') else ''}\n\n")
            if job.get("prop_ctx"):
                about += job["prop_ctx"] + "\n\n"
            if job["tid"]:
                lines = (vault / job["src"]).read_text().split("\n")
                i, j = find_block(lines, job["tid"])
                about += f"The task is in `{job['src']}`:\n\n" + "\n".join(lines[i:j])
            cmd.append(AGENT_PROMPT.format(about=about.strip(), text=job["text"]))
        before = dirty(vault)
        try:
            r = subprocess.run(cmd, cwd=vault, capture_output=True, text=True, timeout=AGENT_TIMEOUT)
            out = json.loads(r.stdout) if r.stdout.strip().startswith("{") else {}
            summary = (out.get("result") or r.stderr or r.stdout or "no output").strip()
            failed = r.returncode != 0 or out.get("is_error")
            job["session"] = out.get("session_id") or job["session"]
        except subprocess.TimeoutExpired:
            summary, failed = f"Timed out after {AGENT_TIMEOUT} s.", True
        add_ids(vault)  # tasks the agent added get 🆔 / ➕ now, not at the next Update
        changed = [p for p, h in dirty(vault).items() if before.get(p) != h]
        sha, warn = commit_push(vault, changed, f"agent: {job['text'][:70]}") if changed else (None, "")
    question = summary.startswith("QUESTION:")
    job.update(status="error" if failed else "done", question=question, commit=sha,
               summary=(summary.removeprefix("QUESTION:").strip()[:600] + (f" ({warn})" if warn else "")),
               anchor=find_anchor(vault, job["tid"], job["orig_text"]))


def claude_bin() -> str | None:
    """Path to a local Claude CLI if installed; None when Cursor-only (no hard-coded fallback)."""
    return shutil.which("claude")


def run_update(vault: Path, job: dict) -> None:
    """Refresh from disk/git: pull, ids, collect, minimal stubs, render. No LLM / no Claude."""
    job["status"] = "running"
    cache = Path(job["cache"])
    with _lock:
        pull(vault, force=True)
        before = dirty(vault)
        id_files = add_ids(vault)
        data = collect(vault)
        cache.mkdir(parents=True, exist_ok=True)
        (cache / "data.json").write_text(json.dumps(data, ensure_ascii=False, indent=1))
        stubs = load_or_build_stubs(cache, data, rewrite=True)
        (cache / "index.html").write_text(render(data, stubs))
        if not (cache / "proposals.json").exists():
            (cache / "proposals.json").write_text('{"candidates": [], "likely_done": []}\n')
        changed = [p for p, h in dirty(vault).items() if before.get(p) != h]
        sha, warn = (commit_push(vault, changed, f"update: {dt.date.today().isoformat()}")
                     if changed else (None, ""))
        n_open = sum(1 for t in data["tasks"] if t["section"] == "Active")
        n_lists = len(data.get("task_lists") or [])
        n_proj = sum(1 for p in data["projects"] if p.get("active"))
        bits = [
            f"**Refreshed from disk/git** (no Claude): {n_open} Active tasks, "
            f"{n_lists} category/project lists, {n_proj} active projects.",
        ]
        if id_files:
            bits.append("- Assigned 🆔 on: " + ", ".join(f"`{f}`" for f in id_files))
        bits.append("- Wrote `dashboard/data.json`, `stubs.json` (pass-through stands), `index.html`.")
        if warn:
            bits.append(f"- ({warn})")
        summary = "\n".join(bits)
    job.update(status="done", commit=sha, finished=time.strftime("%H:%M"),
               summary=summary[:3000])


def start_update(cache: Path) -> str:
    if any(j.get("kind") == "update" and j["status"] in ("queued", "running") for j in _jobs.values()):
        raise LookupError("an update is already running")
    jid = uuid.uuid4().hex[:12]
    _jobs[jid] = {"id": jid, "kind": "update", "cache": cache, "text": "Update", "status": "queued", "summary": "",
                  "commit": None, "anchor": None, "question": False, "tid": "", "box": "", "dismissed": False,
                  "created": time.time(), "finished": ""}
    _queue.put(jid)
    return jid


def last_update() -> dict:
    ups = [j for j in _jobs.values() if j.get("kind") == "update" and not j["dismissed"]
           and time.time() - j["created"] < JOB_TTL]
    j = max(ups, key=lambda j: j["created"], default=None)
    return {k: j[k] for k in ("id", "status", "summary", "commit", "finished")} if j else {}


def worker(vault: Path) -> None:
    while True:
        job = _jobs[_queue.get()]
        try:
            (run_update if job.get("kind") == "update" else run_job)(vault, job)
        except Exception as e:
            job.update(status="error", summary=f"{type(e).__name__}: {e}", anchor=job["tid"])


def new_job(vault: Path, cache: Path, req: dict) -> str:
    text = str(req["text"]).strip()[:2000]
    if "job" in req:  # follow-up in the same Claude session
        parent = _jobs[req["job"]]
        parent["dismissed"] = True
        base = {k: parent[k] for k in ("src", "tid", "orig_text", "box", "session")}
        base["tid"] = parent.get("anchor") or parent["tid"]
    elif req.get("review"):  # answer to a review question; the item may have no task
        item = review_item(cache, str(req["review"]))
        tid = item.get("task") or ""
        src = next((t["source"] for t in open_tasks(vault) if tid and t["id"] == tid), item.get("src", ""))
        if not SRC_RE.match(src):
            raise ValueError(f"bad source {src!r}")
        base = {"src": src, "tid": tid, "box": "review", "session": None, "review_q": item.get("q", ""),
                "why": item.get("why", ""), "orig_text": next((t["text"] for t in open_tasks(vault)
                                                              if tid and t["id"] == tid), "")}
        mark_review(cache, item["id"], f"answered: {text[:80]}")
    elif isinstance(req.get("proposal"), dict):  # instruction about an Update proposal
        pr = req["proposal"]
        if pr.get("kind") == "candidate":
            ctx = (f"The last dashboard Update proposed this new task: {pr.get('text', '')}"
                   + (f" (project `{pr['project']}`)" if pr.get("project") else "")
                   + (f" (due {pr['due']})" if pr.get("due") else "")
                   + ". the user's instruction below decides what happens to it; adding the task as they "
                     "describes is approved.")
            base = {"src": "TASKS.md", "tid": "", "orig_text": ""}
            key = str(pr.get("text", ""))
        elif pr.get("kind") == "done":
            src, tid = str(pr.get("src", "")), str(pr.get("id", ""))
            if not SRC_RE.match(src):
                raise ValueError(f"bad source {src!r}")
            ctx = f"The last dashboard Update proposed that this task is already done ({pr.get('why', '')})."
            base = {"src": src, "tid": tid, "orig_text": next((t["text"] for t in open_tasks(vault)
                                                              if t["id"] == tid), "")}
            key = tid
        else:
            raise ValueError("bad proposal")
        base.update(box="prop", session=None, prop_ctx=ctx)
        drop_proposal(cache, key)
    else:
        if not SRC_RE.match(req["src"]):
            raise ValueError(f"bad source {req['src']!r}")
        base = {"src": req["src"], "tid": req["id"], "box": req.get("box", ""), "session": None,
                "orig_text": next((t["text"] for t in open_tasks(vault)
                                   if t["id"] == req["id"]), "")}
    jid = uuid.uuid4().hex[:12]
    _jobs[jid] = {**base, "id": jid, "text": text, "status": "queued", "summary": "", "commit": None,
                  "anchor": None, "question": False, "dismissed": False, "created": time.time()}
    _queue.put(jid)
    return jid


def public(job: dict) -> dict:
    return {k: job[k] for k in ("id", "text", "status", "summary", "commit", "anchor", "question", "tid", "box")}


_review_lock = threading.Lock()


def review_item(cache: Path, rid: str) -> dict:
    review = json.loads((cache / "review.json").read_text())
    item = next((x for x in review.get("items", []) if x.get("id") == rid), None)
    if item is None:
        raise LookupError(f"review item {rid!r} not found")
    return item


def mark_review(cache: Path, rid: str, answer: str) -> None:
    """Record the answer in review.json (local, not in git); the item leaves the panel."""
    with _review_lock:
        path = cache / "review.json"
        review = json.loads(path.read_text())
        for x in review.get("items", []):
            if x.get("id") == rid:
                x["answered"] = f"{dt.date.today().isoformat()} {answer}"
        path.write_text(json.dumps(review, ensure_ascii=False, indent=1))


def proposals(vault: Path, cache: Path) -> dict:
    """The last Update's candidate tasks and likely-done tasks (still open), for the confirm box."""
    path = cache / "proposals.json"
    try:
        p = json.loads(path.read_text()) if path.exists() else {}
    except json.JSONDecodeError:
        p = {}
    open_by_id = {t["id"]: t for t in open_tasks(vault) if t["id"]}
    done = []
    for x in p.get("likely_done") or []:
        t = open_by_id.get(str(x.get("id", "")))
        if t:
            done.append({"id": t["id"], "src": t["source"], "text": t["text"], "why": str(x.get("why", ""))})
    cands = [{"text": str(c["text"]), "project": str(c.get("project") or ""), "due": str(c.get("due") or "")}
             for c in p.get("candidates") or [] if isinstance(c, dict) and c.get("text")]
    return {"candidates": cands, "likely_done": done}


def drop_proposal(cache: Path, key: str) -> None:
    """Remove one entry (candidate by text, likely-done by id) from proposals.json."""
    with _review_lock:
        path = cache / "proposals.json"
        if not path.exists():
            return
        p = json.loads(path.read_text())
        p["candidates"] = [c for c in p.get("candidates") or [] if c.get("text") != key]
        p["likely_done"] = [x for x in p.get("likely_done") or [] if x.get("id") != key]
        path.write_text(json.dumps(p, ensure_ascii=False, indent=1))


def _category_tasks_src(vault: Path, dom: str) -> str | None:
    """tasks/<cat>/TASKS.md for a domain."""
    cat = next((k for k, v in CATEGORY_DIRS.items() if v == dom), None)
    if cat and (vault / f"tasks/{cat}/TASKS.md").is_file():
        return f"tasks/{cat}/TASKS.md"
    return None


def resolve_add_src(vault: Path, src: str = "", domain: str = "", project: str = "") -> tuple[str, str | None]:
    """Vault-relative TASKS.md path (+ optional [DOMAIN] tag) for a new task.

    Prefer explicit `src`, then project file under tasks/, then category rollup,
    else root TASKS.md. Tag domain on category/root lines only (not project files).
    """
    dom = normalize_domain(domain) if domain else None
    if src:
        if not SRC_RE.match(src) or not src.endswith("TASKS.md"):
            raise ValueError(f"bad source {src!r}")
        if not (vault / src).is_file():
            raise ValueError(f"missing {src}")
        if re.match(rf"tasks/{CAT_FOLDER_RE}/TASKS\.md$", src):
            folder = src.split("/")[1]
            return src, CATEGORY_DIRS.get(folder) or LEGACY_CATEGORY_DIRS.get(folder)
        if re.match(rf"tasks/{CAT_FOLDER_RE}/[\w-]+/TASKS\.md$", src):
            return src, None
        return src, dom

    proj = project.strip() if project else ""
    if proj and not re.fullmatch(r"[\w-]+", proj):
        raise ValueError(f"bad project {proj!r}")
    if proj:
        for tf in discover_task_files(vault):
            if tf["project"] == proj and (not dom or tf["domain"] == dom):
                return tf["source"], None
        if dom and (cat_src := _category_tasks_src(vault, dom)):
            return cat_src, dom
        return "TASKS.md", dom

    if dom:
        if cat_src := _category_tasks_src(vault, dom):
            return cat_src, dom
        raise ValueError(f"no category TASKS.md for {dom}")
    return "TASKS.md", None


def _insert_lines_in_section(lines: list[str], section: str, new_lines: list[str]) -> None:
    """Append new_lines at the end of `## section` (create section at top if missing)."""
    a = next((k for k, l in enumerate(lines) if l.strip() == f"## {section}"), None)
    if a is None:
        lines[:0] = [f"## {section}", *new_lines, ""]
        return
    k = next((k for k in range(a + 1, len(lines)) if lines[k].startswith("## ")), len(lines))
    while k > a + 1 and not lines[k - 1].strip():
        k -= 1
    lines[k:k] = new_lines


def add_task(vault: Path, text: str = "", project: str = "", due: str = "",
             src: str = "", domain: str = "", section: str = "Active",
             texts: list[str] | None = None, urgency: str = "") -> dict:
    """Append one or more open tasks to a list's TASKS.md. One commit.

    `text` may be a Keep-style paste (markdown list / multiple lines); it is
    split with split_task_lines unless `texts` is provided. Target file comes
    from `src`, else domain/project under tasks/, else root TASKS.md.
    `urgency` (anytime/desk/urgent) invents 📅 like ⇢ when `due` is empty —
    used by Keep-style add inside Today boxes.
    """
    if due and not re.fullmatch(DATE, due):
        raise ValueError(f"bad date {due!r}")
    urgency = (urgency or "").strip()
    if urgency and urgency not in URGENCY_BUCKETS:
        raise ValueError(f"bad urgency {urgency!r}")
    section = section or "Active"
    if section not in WRITE_SECTIONS:
        raise ValueError(f"bad section {section!r}")
    items = list(texts) if texts is not None else split_task_lines(text if isinstance(text, str) else "")
    items = [re.sub(r"\s+", " ", t).strip()[:400] for t in items]
    items = [t for t in items if t]
    if not items:
        raise ValueError("empty or too long task text")
    if len(items) > 50:
        raise ValueError("too many tasks in one paste (max 50)")
    proj = project.strip() if project else ""
    if proj and not re.fullmatch(r"[\w-]+", proj):
        proj = ""
    stamp_due = due or (urgency_due_date(urgency) if urgency else "")
    with _lock:
        pull(vault, force=True)
        rel, tag_dom = resolve_add_src(vault, src=src, domain=domain, project=proj)
        # Memory link only when writing outside a dedicated project TASKS.md.
        link_proj = ""
        if proj and (vault / f"memory/projects/{proj}.md").exists():
            if not re.match(r"tasks/[^/]+/[^/]+/TASKS\.md$", rel):
                link_proj = proj
        path = vault / rel
        lines = path.read_text().split("\n") if path.is_file() else [f"# {rel}", "", f"## {section}", ""]
        used = {m.group(1) for f in task_list_files(vault) if f.is_file()
                for m in ID_RE.finditer(f.read_text())}
        used |= {m.group(1) for m in ID_RE.finditer("\n".join(lines))}
        today = dt.date.today().isoformat()
        new_lines = []
        for t in items:
            tid = uuid.uuid4().hex[:6]
            while tid in used:
                tid = uuid.uuid4().hex[:6]
            used.add(tid)
            body = t
            if tag_dom and not re.search(rf"\[{DOMAIN_TAG}\]", body):
                body = f"[{tag_dom}] {body}"
            line = (f"- [ ] {body}"
                    + (f" → `memory/projects/{link_proj}.md`" if link_proj else "")
                    + (f" 📅 {stamp_due}" if stamp_due else "")
                    + f" ➕ {today} 🆔 {tid}")
            new_lines.append(line)
        _insert_lines_in_section(lines, section, new_lines)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("\n".join(lines))
        n = len(new_lines)
        msg = f"add: {items[0][:70]}" if n == 1 else f"add: {n} tasks ({items[0][:40]})"
        sha, warn = commit_push(vault, [rel], msg)
        label = f"Added: {items[0][:60]}" if n == 1 else f"Added {n} tasks"
        return {"ok": True, "text": items[0], "texts": items, "n": n, "src": rel,
                "due": stamp_due, "urgency": urgency or None,
                "label": label, "commit": sha, "warn": warn}


def provenance(vault: Path, src: str, tid: str) -> dict:
    """Where a task came from: the commit that added its text, its last change, inbox notes
    that mention it."""
    if not SRC_RE.match(src):
        raise ValueError(f"bad source {src!r}")
    lines = (vault / src).read_text().split("\n")
    i, _ = find_block(lines, tid)
    raw = TASK_RE.match(lines[i]).group(1)
    needle = re.sub(rf"^(‼️\s*)?(\[{DOMAIN_TAG}\]\s*)?", "", raw)[:40].strip()
    base = vault_url(vault)
    commit_base = re.sub(r"/blob/[^/]+$", "/commit/", base)

    def commit(line: str) -> dict | None:
        if not line:
            return None
        sha, date, subject = line.split("|", 2)
        return {"sha": sha[:7], "date": date, "subject": subject[:100], "url": commit_base + sha}
    first = git(vault, "log", "--reverse", "--format=%H|%cs|%s", "-S", needle, "--").stdout.splitlines()
    ignore = ["--ignore-revs-file", str(vault / BLAME_IGNORE)] if (vault / BLAME_IGNORE).exists() else []
    blame = git(vault, "blame", "--porcelain", *ignore, "-L", f"{i + 1},{i + 1}", "--", src).stdout.splitlines()
    sha = blame[0].split()[0] if blame else ""
    last = git(vault, "log", "-1", "--format=%H|%cs|%s", sha, "--").stdout.strip() if sha.strip("0") else ""
    notes = git(vault, "grep", "-l", "-F", needle[:30], "--", "inbox").stdout.splitlines()
    return {"added": parse_task(raw, src)["added"], "first": commit(first[0] if first else ""),
            "last": commit(last), "file": f"{base}/{src}#L{i + 1}",
            "notes": [{"path": n, "url": f"{base}/{n}"} for n in notes]}


def live_page(vault: Path, cache: Path) -> str:
    if _lock.acquire(blocking=False):
        try:
            pull(vault)
        finally:
            _lock.release()
    cached = json.loads((cache / "data.json").read_text()) if (cache / "data.json").exists() else {}
    activity = {a["repo"]: a for p in cached.get("projects", []) for a in p.get("activity", [])}
    data = collect(vault, activity)
    data["generated_at"] = f"live; GitHub and summaries from {cached.get('generated_at', 'never')}"
    stubs = load_or_build_stubs(cache, data, rewrite=False)
    review = json.loads((cache / "review.json").read_text()) if (cache / "review.json").exists() else None
    return render(data, stubs, live=True, review=review)


class Handler(BaseHTTPRequestHandler):
    def send(self, code: int, body, ctype: str = "application/json") -> None:
        b = (body if isinstance(body, str) else json.dumps(body, ensure_ascii=False)).encode()
        self.send_response(code)
        self.send_header("Content-Type", f"{ctype}; charset=utf-8")
        self.send_header("Content-Length", str(len(b)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(b)

    def do_GET(self):
        path = self.path.split("?")[0]
        vault, cache = self.server.vault, self.server.cache
        try:
            if path in ("/", "/index.html"):
                return self.send(200, live_page(vault, cache), "text/html")
            if path.lstrip("/") in STATIC_FILES:
                name = path.lstrip("/")
                return self.send_bytes((STATIC_DIR / name).read_bytes(), STATIC_FILES[name], 86400)
            if path.startswith("/static/"):
                f = static_file(path[len("/static/"):])
                if not f:
                    return self.send(404, {"error": "not found"})
                return self.send_bytes(f.read_bytes(), STATIC_TYPES[f.suffix.lower()], 3600)
            if path == "/notes" or path.startswith("/notes/"):
                return self.notes_get(path, vault)
            if path == "/jobs":
                now = time.time()
                return self.send(200, [public(j) for j in _jobs.values() if j.get("kind") != "update"
                                       and not j["dismissed"] and now - j["created"] < JOB_TTL])
            if path == "/update":
                return self.send(200, last_update())
            if path == "/proposals":
                return self.send(200, proposals(vault, cache))
            if path.startswith("/job/") and path[5:] in _jobs:
                return self.send(200, public(_jobs[path[5:]]))
            if path == "/source":
                q = parse_qs(urlsplit(self.path).query)
                try:
                    return self.send(200, provenance(vault, q["src"][0], q["id"][0]))
                except (LookupError, ValueError, KeyError) as e:
                    return self.send(200, {"error": str(e)})
            self.send(404, {"error": "not found"})
        except Exception as e:
            self.send(500, f"<pre>{html.escape(repr(e))}</pre>", "text/html")

    def send_bytes(self, body: bytes, ctype: str, max_age: int = 0) -> None:
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", f"max-age={max_age}" if max_age else "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(body)

    def host_ok(self) -> bool:
        """Only requests addressed to this loopback server (blocks DNS-rebinding reads)."""
        host = (self.headers.get("Host") or "").rsplit(":", 1)[0]
        return host in ("127.0.0.1", "localhost", "[::1]")

    def notes_get(self, path: str, vault: Path):
        if not self.host_ok():
            return self.send(403, {"error": "bad host"})
        q = {k: v[0] for k, v in parse_qs(urlsplit(self.path).query).items()}
        try:
            if path in ("/notes", "/notes/"):
                return self.send_bytes((STATIC_DIR / "notes" / "index.html").read_bytes(), "text/html; charset=utf-8")
            if path == "/notes/api/list":
                notes_refresh(vault)
                root = notes_root(vault)
                return self.send(200, {"projects": notes_projects(vault), "conflicts": _notes_conflicts,
                                       "dirty": bool(dirty_notes(vault)), "root": str(root),
                                       "repo_ok": (root / ".git").exists()})
            if path == "/notes/api/note":
                return self.send(200, note_read(vault, q.get("key", ""), q.get("name", "")))
            if path == "/notes/api/tasks":
                return self.send(200, {"tasks": notes_tasks(vault, q.get("key", ""))})
            if path == "/notes/file":
                body, ctype = note_asset(vault, q.get("key", ""), q.get("path", ""))
                return self.send_bytes(body, ctype, 3600)
        except NotesPathError as e:
            return self.send(400, {"error": str(e)})
        except LookupError as e:
            return self.send(404, {"error": str(e)})
        return self.send(404, {"error": "not found"})

    def notes_post(self, vault: Path):
        if not self.host_ok():
            return self.send(403, {"error": "bad host"})
        n = int(self.headers.get("Content-Length", 0))
        if self.path.startswith("/notes/api/upload"):
            if n > MAX_UPLOAD:
                return self.send(413, {"error": "image too large"})
            q = {k: v[0] for k, v in parse_qs(urlsplit(self.path).query).items()}
            return self.send(200, note_upload(vault, q.get("key", ""), self.headers.get("Content-Type", ""),
                                              self.rfile.read(n)))
        req = json.loads(self.rfile.read(n) or b"{}")
        path = self.path.split("?")[0]
        if path == "/notes/api/save":
            return self.send(200, note_save(vault, str(req["key"]), str(req["name"]), req["text"], req.get("base")))
        if path == "/notes/api/create":
            return self.send(200, note_create(vault, str(req["key"]), str(req["name"]).strip()))
        if path == "/notes/api/rename":
            return self.send(200, note_rename(vault, str(req["key"]), str(req["name"]), str(req["new"]).strip()))
        if path == "/notes/api/sync":
            return self.send(200, notes_sync(vault))
        if path == "/notes/api/conflicts/dismiss":
            _notes_conflicts.clear()
            return self.send(200, {"ok": True})
        return self.send(404, {"error": "not found"})

    def do_POST(self):
        host = self.headers.get("Host", "")
        if self.headers.get("Origin") not in (None, f"http://{host}"):
            return self.send(403, {"error": "cross-origin request refused"})
        vault = self.server.vault
        if self.path.startswith("/notes/"):
            try:
                return self.notes_post(vault)
            except NotesPathError as e:
                return self.send(400, {"error": str(e)})
            except LookupError as e:
                return self.send(409, {"error": str(e)})
            except Exception as e:
                return self.send(400, {"error": f"{type(e).__name__}: {e}"})
        try:
            req = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))))
            if self.path == "/act":
                res = act(vault, str(req["src"]), str(req["id"]), str(req["op"]), str(req.get("arg", "")))
                print(f"{res['label']} {res['warn']}", file=sys.stderr)
                if req.get("review"):
                    mark_review(self.server.cache, str(req["review"]), res["label"])
                if req.get("proposal"):
                    drop_proposal(self.server.cache, str(req["proposal"]))
            elif self.path == "/relocate":
                items = req.get("items") or []
                if not isinstance(items, list):
                    raise ValueError("items must be a list")
                res = relocate_many(
                    vault,
                    [{"src": str(x.get("src", "")), "id": str(x.get("id", ""))} for x in items],
                    dest=str(req.get("dest", "")),
                    domain=str(req.get("domain", "")),
                    project=str(req.get("project", "")),
                    create=bool(req.get("create")),
                )
                print(f"{res['label']} {res['warn']}", file=sys.stderr)
            elif self.path == "/done-batch":
                items = req.get("items") or []
                if not isinstance(items, list):
                    raise ValueError("items must be a list")
                res = done_many(
                    vault,
                    [{"src": str(x.get("src", "")), "id": str(x.get("id", ""))} for x in items],
                )
                print(f"{res['label']} {res['warn']}", file=sys.stderr)
            elif self.path == "/urgency-batch":
                items = req.get("items") or []
                if not isinstance(items, list):
                    raise ValueError("items must be a list")
                res = urgency_many(
                    vault,
                    [{"src": str(x.get("src", "")), "id": str(x.get("id", ""))} for x in items],
                    str(req.get("urgency", "")),
                )
                print(f"{res['label']} {res['warn']}", file=sys.stderr)
            elif self.path == "/add":
                raw_texts = req.get("texts")
                texts = [str(x) for x in raw_texts] if isinstance(raw_texts, list) else None
                res = add_task(
                    vault,
                    text=str(req.get("text", "")),
                    project=str(req.get("project", "")),
                    due=str(req.get("due", "")),
                    src=str(req.get("src", "")),
                    domain=str(req.get("domain", "")),
                    section=str(req.get("section", "") or "Active"),
                    texts=texts,
                    urgency=str(req.get("urgency", "")),
                )
                drop_proposal(self.server.cache, str(req.get("text") or (res.get("text") or "")))
            elif self.path == "/proposal/dismiss":
                drop_proposal(self.server.cache, str(req["key"]))
                res = {"ok": True}
            elif self.path == "/review":
                mark_review(self.server.cache, str(req["review"]),
                            str(req.get("answer") or "skipped")[:80])
                res = {"ok": True}
            elif self.path == "/undo":
                res = undo(vault, str(req["commit"]))
            elif self.path == "/code-todo/done":
                res = code_todo_done(self.server.cache, str(req["repo"]), str(req["path"]), str(req["key"]))
                print(f"{res['label']} {res['commit']}", file=sys.stderr)
            elif self.path == "/code-todo/undo":
                res = code_todo_undo(self.server.cache, str(req["commit"]))
            elif self.path == "/ask":
                res = {"job": new_job(vault, self.server.cache, req)}
            elif self.path == "/update":
                res = {"job": start_update(self.server.cache)}
            elif self.path == "/pull":
                with _lock:
                    pull(vault, force=True)
                res = {"ok": True, "pulled_at": dt.datetime.now().isoformat(timespec="seconds")}
            elif self.path == "/dismiss":
                _jobs.get(str(req["job"]), {})["dismissed"] = True
                res = {"ok": True}
            else:
                return self.send(404, {"error": "not found"})
            self.send(200, res)
        except LookupError as e:
            self.send(409, {"error": str(e)})
        except Exception as e:
            self.send(400, {"error": f"{type(e).__name__}: {e}"})

    def log_message(self, fmt, *args):
        pass


def serve(vault: Path, cache: Path, port: int) -> None:
    srv = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    srv.vault, srv.cache = vault, cache
    threading.Thread(target=worker, args=(vault,), daemon=True).start()
    print(f"dashboard live at http://127.0.0.1:{port}/", file=sys.stderr)
    srv.serve_forever()


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser("collect")
    c.add_argument("--vault", type=Path, default=DEFAULT_VAULT)
    c.add_argument("--review", action="store_true", help="add review_candidates")
    c.add_argument("-o", "--out", type=Path)
    i = sub.add_parser("ids")
    i.add_argument("--vault", type=Path, default=DEFAULT_VAULT)
    r = sub.add_parser("render")
    r.add_argument("data", type=Path)
    r.add_argument("stubs", type=Path, nargs="?")
    r.add_argument("-o", "--out", type=Path)
    s = sub.add_parser("serve")
    s.add_argument("--vault", type=Path, default=DEFAULT_VAULT)
    s.add_argument("--cache", type=Path, help="dir with data.json, stubs.json (default <vault>/dashboard)")
    s.add_argument("--port", type=int, default=8765)
    ln = sub.add_parser("link-notes", help="show a project's vault notes as progress/ in its code folder")
    ln.add_argument("project", help="slug (website) or key (work/website)")
    ln.add_argument("code", type=Path, help="the project's code folder (git repo)")
    ln.add_argument("--vault", type=Path, default=DEFAULT_VAULT)
    im = sub.add_parser("import-obsidian", help="copy Obsidian notes into the vault (source is only read)")
    im.add_argument("src", type=Path, help="Obsidian vault folder")
    im.add_argument("mapping", type=Path, help='JSON {"Note.md": "work/website" | null}')
    im.add_argument("--vault", type=Path, default=DEFAULT_VAULT)
    im.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()
    if a.cmd == "link-notes":
        return print(json.dumps(link_notes(a.vault, a.project, a.code), indent=1, ensure_ascii=False))
    if a.cmd == "import-obsidian":
        rep_ = import_obsidian(a.src, a.vault, json.loads(a.mapping.read_text()), dry_run=a.dry_run)
        return print(json.dumps(rep_, indent=1, ensure_ascii=False))
    if a.cmd == "serve":
        return serve(a.vault, a.cache or a.vault / "dashboard", a.port)
    if a.cmd == "ids":
        return print("\n".join(add_ids(a.vault)))
    if a.cmd == "collect":
        out = json.dumps(collect(a.vault, review=a.review), ensure_ascii=False, indent=1)
    else:
        stubs = json.loads(a.stubs.read_text()) if a.stubs else {}
        out = render(json.loads(a.data.read_text()), stubs)
    if a.out:
        a.out.parent.mkdir(parents=True, exist_ok=True)
        a.out.write_text(out)
        print(f"wrote {a.out}", file=sys.stderr)
    else:
        print(out)


if __name__ == "__main__":
    main()
