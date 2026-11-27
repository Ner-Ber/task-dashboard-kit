---
name: vault-notes
description: Read the user's private progress notes (lab-diary style, one folder per project) and add dated entries to them. Use when the user says "add to my progress notes", "note that …", "log this in <project> notes", "what did I write about <topic>", "summarise my <project> notes", or asks about past meetings, results or decisions for a project.
---

# Progress notes

Notes are markdown files in the user's private **notes repo** (`$ASSISTANT_NOTES`
locally, or `<OWNER>/<NOTES_REPO>` on GitHub, branch `main`). The dashboard's Notes
tab edits them, and each code repository can show its project's folder as a
git-ignored `progress/` link.

## Where

- `<category>/<project>/<Note>.md` — a project's notes (usually `Progress.md`)
- `<category>/<Note>.md` — category-wide notes
- `<category>/<project>/attachments/` — images, referenced as `![](attachments/<file>)`
- `PROJECTS.md` — folder ↔ project name ↔ code repos. Inside a code repo, the
  `progress/` link already points at the right folder.
- `*.conflict-YYYYMMDD-HHMMSS.md` — the other side of an edit conflict. Read it as
  a parallel version; never write to it. The user resolves it in the dashboard's Notes tab
  (diff view, ⇄ Merge: per dated entry, newer edit wins; × Discard).

## Read

Open the relevant notes, answer from them, and say which note you used.

## Add an entry

1. Sync first: run `<notes repo>/.notes/sync.sh --pull` if it exists (or `git pull`).
2. Pick the note the user names; else the project's main log (`Progress.md`);
   create it with `# Progress` as the first line if missing.
3. Entries are dated sections `## Mon D, YYYY` (e.g. `## Nov 20, 2026`), optionally
   ` - Meeting with …`, **newest on top**: insert the new section above the first
   dated `##` heading. If today's heading exists, add under it.
4. Use the user's words; don't embellish. Math as `$…$` / `$$…$$`. Images go into
   that folder's `attachments/` and are linked as `![](attachments/<file>)`.
5. Change only your insertion. Then sync: `<notes repo>/.notes/sync.sh` (or commit
   `notes: <folder>/<Note>` and push). If it reports a CONFLICT, tell the user.

## Don't

- Never copy notes or their images into a code repository, commit, PR or issue,
  and never `git add` a `progress/` link.
- Don't rewrite past entries, rename or delete notes unless asked.
- Tasks are a different repo: use the `vault-tasks` skill.
