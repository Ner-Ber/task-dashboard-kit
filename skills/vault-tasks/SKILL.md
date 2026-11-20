---
name: vault-tasks
description: Add, complete, reschedule, move and look up the user's tasks in their private task vault (a git repo of markdown task lists that the task dashboard reads). Use when the user says "add a task", "remind me to…", "mark … done", "what's due", "what's on my plate for <project>", "snooze …", "move … to <project>", or asks about their to-dos.
---

# Vault tasks

The user keeps tasks as markdown checkboxes in a private git repository (the
**vault**). A local dashboard shows them; you edit the same files. Configure once:

- **Vault**: `$ASSISTANT_VAULT` (a local clone), or the GitHub repo `<OWNER>/<VAULT_REPO>`
  (branch `main`) when you work through GitHub instead of a checkout.
- **Categories**: `CATEGORIES` in the kit's `.env`, e.g. `work:Work,side:Side,life:Life`
  (folder `tasks/work/` ↔ label `Work`). Read `.env` or list `tasks/` to learn them.

## Where tasks live

| File | Holds |
|---|---|
| `TASKS.md` | "Today" / anything not filed yet |
| `tasks/<category>/TASKS.md` | category-wide tasks; each line tagged `[Label]` |
| `tasks/<category>/<project>/TASKS.md` | one project's tasks (no tag needed) |
| `inbox/*.md` | quick captures waiting to be filed |

A project's display name is the first `# ` heading of its `TASKS.md`. Project
metadata (code repos, status) is in `memory/projects/<project>.md`.

Each list has the sections `## Active`, `## Waiting On`, `## Someday`, `## Done`.

## Task line format

```
- [ ] [Work] Book the planning room ➕ 2026-11-10 🆔 b20e11 📅 2026-11-27
```

| Marker | Meaning |
|---|---|
| `- [ ]` / `- [x]` | open / done |
| `[Label]` | category tag (category lists and `TASKS.md`; optional in project lists) |
| `➕ YYYY-MM-DD` | date added (always set it) |
| `🆔 xxxxxx` | 6 lowercase hex chars, unique across the vault (always set it) |
| `📅 YYYY-MM-DD` | due date: ≤ 2 days away → **Urgent**, ≤ 14 days → **On the desk**, none → **Any time** |
| `⏳ YYYY-MM-DD` | snoozed: hidden from the Today boxes until that date |
| `✅ YYYY-MM-DD` | date completed (on `- [x]` lines) |
| `‼️` | high priority (optional) |

Keep each task on one line. Don't add other emoji markers.

## How to

- **Add**: choose the list. If the task names a project, use that project's
  `TASKS.md`; else the category's; if unsure, ask. Append under `## Active` (or
  `## Waiting On` / `## Someday` if the user says so) with `➕` today and a fresh
  `🆔` (check it's not already used: search the vault for `🆔 <id>`). Add `📅` only
  if the user gave a date ("by Friday" → that date).
- **Complete**: change `- [ ]` to `- [x]`, append `✅` today, move the line under
  `## Done`. Find the task by its text or `🆔`; if several match, ask.
- **Reschedule / snooze**: replace or add `📅` / `⏳`. Remove `⏳` when the user
  un-snoozes.
- **Move**: cut the line from one list and paste it into the other, keeping its
  `➕` and `🆔`. Add or remove the `[Label]` tag to suit the destination.
- **New project**: create `tasks/<category>/<slug>/TASKS.md` (heading = project name,
  the four sections), and `memory/projects/<slug>.md` if the user gives a repo/status.
- **Query**: read the lists and answer: overdue (`📅` before today), due soon,
  per project, waiting on. Mention the file each task is in when it helps.

## Sync

The vault is git-as-sync: pull before editing, then commit and push small changes
straight to `main` (`git pull --rebase`, `git commit -m "tasks: <what>"`, `git push`).
The dashboard pushes too, so if a push is rejected, pull and retry. Never force-push.

## Don't

- Don't rewrite or reorder other lines, or reformat the file.
- Don't invent dates, projects or people. Ask instead.
- Never copy vault content into a code repository, issue or PR.
- Progress notes are a separate repo: use the `vault-notes` skill for those.
