# AGENTS.md: task dashboard kit

Instructions for any coding agent (Codex, Copilot, Cursor, Gemini CLI, Claude Code, …)
working with this kit, a task vault, or a progress-notes repo. This is the source of
truth; `CLAUDE.md` and `.gemini/settings.json` point here.

## The pieces

| Repo | What | Privacy |
|---|---|---|
| **this kit** | dashboard app (`app/`), scripts, skills, templates, docs | public-safe: no personal data, ever |
| **vault** (`$ASSISTANT_VAULT`) | task lists, project metadata, inbox | **private** |
| **notes** (`$ASSISTANT_NOTES`) | progress notes + images, one folder per project | **private** |
| **code repos** | your projects; may be public | notes appear here only as a git-ignored `progress/` link |

Settings live in the kit's `.env` (template: `.env.example`). Never commit `.env`.

## Vault layout

```
TASKS.md                               Today / unfiled
tasks/<category>/TASKS.md              category list, lines tagged [Label]
tasks/<category>/<project>/TASKS.md    project list; first "# " heading = project name
memory/projects/<project>.md           project metadata (repos, status, next action)
memory/me.md                           the user's preferences for agents (user-written)
inbox/*.md                             quick captures → file into a list, move to inbox/processed/
dashboard/                             generated cache (don't edit)
```

`<category>` folders and labels come from `CATEGORIES` in `.env`
(e.g. `work:Work,side:Side,life:Life`). Every list has `## Active`, `## Waiting On`,
`## Someday`, `## Done`.

### Task line

```
- [ ] [Work] Book the planning room ➕ 2026-11-10 🆔 b20e11 📅 2026-11-27
```

| Marker | Meaning |
|---|---|
| `- [ ]` / `- [x]` | open / done |
| `[Label]` | category tag: required in category lists and `TASKS.md`, optional in project lists |
| `➕ YYYY-MM-DD` | added (always set) |
| `🆔 xxxxxx` | 6 lowercase hex, unique in the vault (always set; check with a search) |
| `📅 YYYY-MM-DD` | due: ≤ 2 days → Urgent, ≤ 14 days → On the desk, none → Any time |
| `⏳ YYYY-MM-DD` | snoozed until |
| `✅ YYYY-MM-DD` | completed (on `- [x]`) |
| `- [-]` + `❌ YYYY-MM-DD` | dropped (not done, no longer needed); kept under `## Done` |
| `‼️` | high priority (optional) |
| `🔗 <ref>` | source of the task (optional, several allowed): `gmail:<threadId>`, `sheet:<id>[#gid=N]`, `doc:<id>`, `drive:<id>`, an `https://` URL, or a vault note path (`inbox/…md`, `memory/…md`) |
| `👀 YYYY-MM-DD` | last confirmed still relevant (set by the weekly review) |

**Add**: append under `## Active` of the right list with `➕` today and a new `🆔`.
**Complete**: `- [x]`, append `✅` today, move under `## Done`.
**Snooze / due**: set or replace `⏳` / `📅`. **Move**: cut and paste the line, keeping
`➕` and `🆔`. **New project**: `tasks/<category>/<slug>/TASKS.md` with the heading and
the four sections, plus `memory/projects/<slug>.md` if it has a repo. One task per
line; don't reformat other lines. The `vault-tasks` skill has the full procedure.

### Project metadata (`memory/projects/<slug>.md`)

```
# Website relaunch
**Domain:** Work
**Status:** active            (active | paused | submitted | done; done/paused hide the card)
**Repo:** `example-org/website` `example-org/api@feature/v2`
**Related repos:** `example-org/design`
**Local:** `~/code/website`
## Next action
Draft the homepage copy.
```

`**Repo:**` links a project to its code (`owner/name`, optionally `@branch` when several
projects share one repo). The dashboard shows those repos' commits, PRs and code TODO
files (`*todo*.md`, `*todo*.txt`) on the project card.

## Progress notes

```
<category>/<project>/<Note>.md          e.g. work/website/Progress.md
<category>/<project>/attachments/<img>  images of that folder's notes
<category>/<Note>.md                    category-wide notes
PROJECTS.md                             folder ↔ project ↔ code repos (generated; don't edit)
.notes/sync.sh                          commit + pull --rebase + push, conflict-safe
```

- A note belongs to a project by **folder**: notes folder `work/website/` ↔ vault list
  `tasks/work/website/`. Same names, different repos.
- Entries: `## Mon D, YYYY` (optionally ` - Meeting with …`), **newest on top**.
  Insert above the first dated heading, or under today's if it exists. Keep the user's words.
- Images: save to `attachments/`, link as `![](attachments/<file>)`. The dashboard names
  pasted images `YYYY-MM-DD-HHMMSS.webp`. Math: `$…$`, `$$…$$`.
- `*.conflict-YYYYMMDD-HHMMSS.md` is the other version of an edit conflict. Read it,
  never write it. The user resolves it in the dashboard's Notes tab (diff view, ⇄ Merge per dated
  entry, × Discard) or by hand.
- After editing notes, run `.notes/sync.sh` (or commit `notes: <folder>/<Note>` and push).
  Before reading, `.notes/sync.sh --pull`.

## Inside a code repository

If the repo has a `progress/` folder, it is a **symlink** to that project's notes folder,
hidden from git by a line `/progress` in `.git/info/exclude`. Read it for context. When
you edit notes there, sync with `<notes repo>/.notes/sync.sh`
(`readlink -f progress` shows where the notes repo is).

## Rules (all agents)

1. **Never put vault or notes content into a code repository**: no copying files,
   images or excerpts into tracked files, commits, PRs, issues or CI logs. Never
   `git add progress/`, never add it to `.gitignore` (that file is committed; use
   `.git/info/exclude`).
2. Vault and notes are git-as-sync: small commits straight to `main`, pull before
   editing, never force-push, never rewrite history.
3. Don't invent tasks, dates, people or projects. Ask when unsure which list or note.
4. No secrets anywhere: tokens live in `.env` (git-ignored) or the tools' own logins.
5. In this kit: only generic sample data. Run `scripts/privacy-check.sh` before committing.

## Working on the kit itself

- `app/dashboard.py` is a single-file Python app (standard library only), run with
  `uv run app/dashboard.py serve`. Front-end libraries are vendored in
  `app/static/vendor/`: no CDNs.
- `app/dashboard.py`, `scripts/open-dashboard.sh`, `scripts/install-windows-shortcuts.sh`,
  `tests/test_notes.py`, `app/static/` and `templates/notes/.notes/*` are **generated** by
  `scripts/export-from-source.sh` from the maintainer's own setup (see `docs/UPDATING.md`).
  Change them upstream, or the next export overwrites your edit.
- Tests: `uv run --with pytest pytest -q tests/`.
