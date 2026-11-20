Set up my private progress notes in this workspace. Work through the steps below, show me what you found at each decision point, and ask before anything marked ASK. Don't commit anything to this project's repo.

## Background
- My progress notes live in the PRIVATE GitHub repo `<OWNER>/<NOTES_REPO>`: one folder per project (e.g. `<category>/<project>/Progress.md`, images in `<that folder>/attachments/`). `PROJECTS.md` at its root maps every notes folder to that project's code repo(s) as `owner/repo` or `owner/repo@branch`.
- Goal: a `progress/` folder in this workspace that shows THIS project's notes. It must be invisible to this project's git, because some of my code repos are public.
- Sync goes through `.notes/sync.sh` in the notes repo. It commits, pulls with rebase and pushes. If a note changed in two places, it keeps the local text and saves the other version as `<note>.conflict-<timestamp>.md`, so nothing is ever lost.

## 1. The notes clone (once per machine)
- Use `NOTES_DIR=~/<NOTES_REPO>`. If it already exists and is a clone of `<OWNER>/<NOTES_REPO>`, run `"$NOTES_DIR/.notes/sync.sh" --pull` and go to step 2.
- Otherwise, test access first: `git ls-remote git@github.com:<OWNER>/<NOTES_REPO>.git` (or the https URL if this machine uses https for GitHub).
  - If access works, clone it to `$NOTES_DIR`.
  - If it fails, STOP and ASK me. Offer to create a dedicated key (`ssh-keygen -t ed25519 -f ~/.ssh/notes_repo -N "" -C "notes-repo@$(hostname -s)"`), show me the PUBLIC key so I can add it myself as a deploy key with write access on the repo, then set up a `Host github-notes` entry in `~/.ssh/config` that uses it, and clone through `git@github-notes:<OWNER>/<NOTES_REPO>.git`.
  - Never print or copy a private key.
- Make the clone private to me: `chmod 700 "$NOTES_DIR"`. Show me the permissions of `$HOME` (`stat -c '%A %U' ~`). If group or others can read or enter it, tell me. Don't change `$HOME` permissions without asking.
- Make sure `git config user.name` and `user.email` are set in the clone. If they aren't, ASK me for them.

## 2. Which notes folder belongs to this workspace
- In this workspace's git repo (the top level: `git rev-parse --show-toplevel`), collect `git remote get-url origin` (and any other remotes) as `owner/repo`, plus `git branch --show-current`.
- Read `$NOTES_DIR/PROJECTS.md` and find the rows whose Code column lists this `owner/repo`.
  - If one row lists `owner/repo@<this branch>`, that's the one.
  - A row with no `@branch` matches any branch.
  - Several repos or worktrees can share one code repo on different branches, so the branch decides.
- Exactly one match: use its folder. No match or several: ASK me and show the candidates. Don't create new folders in the notes repo. New projects are created from my dashboard.

## 3. The `progress/` link (per workspace)
- At the repo top level, create the symlink `progress` → `$NOTES_DIR/<folder>`. If `progress` already exists and isn't exactly this link, STOP and ASK.
- Hide it from git locally, never through `.gitignore`:
  - Find the exclude file with `git rev-parse --git-path info/exclude`. This works in linked worktrees too.
  - Append `/progress` and `/.cursor/rules/progress-notes.mdc` (if not already there), with a comment saying they come from my private notes.
- Verify: `git status --porcelain --untracked-files=all -- progress .cursor/rules/progress-notes.mdc` must print nothing, and `git check-ignore -v progress` must name the exclude file. Show me both outputs.
- If this workspace is a multi-root `.code-workspace` file that lives outside the repo, also add `$NOTES_DIR/<folder>` as a folder named `progress notes`. Never edit a workspace file that's tracked in git.

## 4. A Cursor rule for agents (per workspace, git-ignored)
Create `.cursor/rules/progress-notes.mdc` with `alwaysApply: true`, saying in my words:
- This project's progress notes are in `progress/`, a link to my private notes repo. Read them for context: past results, decisions, meeting notes.
- Before reading or editing notes, run `<NOTES_DIR>/.notes/sync.sh --pull`. After editing any file under `progress/`, run `<NOTES_DIR>/.notes/sync.sh`. If it reports a CONFLICT, tell me the file names.
- Format: dated entries `## Mon D, YYYY` (e.g. `## Nov 20, 2026`), newest on top: insert above the first dated heading. Add under today's heading if it already exists. Keep my wording and don't rewrite old entries. Images go in `progress/attachments/` as `![](attachments/<file>)`, math as `$…$` / `$$…$$`.
- Never copy note contents or images into this repo's tracked files, commits, PRs or issues. Never `git add` `progress/`. Never commit, push or rewrite history in the notes repo yourself; `.notes/sync.sh` does that.

## 4b. Image paste into notes (once per machine)
Pasting a screenshot into a note should save the image under the note's `attachments/` folder and insert `![](attachments/<file>)`, the same format the dashboard uses. Set this in Cursor's settings for THIS machine (for a remote/SSH machine, the Remote "Machine" settings: `~/.cursor-server/data/Machine/settings.json`). Never use a tracked `.vscode/settings.json`. Merge the keys, don't overwrite the file:
```json
"markdown.editor.filePaste.enabled": "always",
"markdown.editor.filePaste.copyIntoWorkspace": "mediaFiles",
"markdown.copyFiles.destination": {
  "**/progress/**/*.md": "${documentDirName}/attachments/${documentBaseName}-${unixTime}.${fileExtName}"
},
"markdown.copyFiles.overwriteBehavior": "nameIncrementally"
```
Check these setting names against Cursor's current Markdown settings and adapt if they differ. Then ask me to paste one image into a note to test: the image file must appear in `progress/attachments/` and the note must get an `![...](attachments/...)` line. If only a bare file name like `image.png` appears, the clipboard held text rather than image data. Tell me, and suggest pasting with plain Ctrl+V (not Ctrl+Shift+V), or pasting the image in the dashboard's Notes tab instead.

## 5. Hooks so notes sync on their own (once per machine, user level)
Use Cursor's user-level hooks (`~/.cursor/hooks.json` and a script in `~/.cursor/hooks/`), so no project repo is touched. Check Cursor's current hooks documentation for the exact event names and input/output format before writing them, and adapt if they differ from what I describe:
- **After the agent edits a file** (e.g. `afterFileEdit`): if the edited path is inside a `progress/` link or inside `$NOTES_DIR`, run `$NOTES_DIR/.notes/sync.sh` in the background with a short delay (e.g. `(sleep 20; sync.sh) >/dev/null 2>&1 &`). The script locks itself, so overlapping runs are safe.
- **When the agent finishes** (e.g. `stop`): if `git -C $NOTES_DIR status --porcelain` isn't empty, run the sync. That also catches notes I typed by hand during the session.
- **Before my prompt is sent** (e.g. `beforeSubmitPrompt`): run `sync.sh --pull` in the background, so the agent sees fresh notes.
- Hooks must never block or fail the agent: always exit 0 and send output to a log, e.g. `~/.cursor/hooks/notes-sync.log`.
- Merge into any existing `~/.cursor/hooks.json`; don't overwrite other hooks.

Edits I make by hand without talking to the agent aren't seen by hooks. As a fallback, ASK me whether to add a cron job (`*/15 * * * * $NOTES_DIR/.notes/sync.sh >/dev/null 2>&1`). First check whether `crontab` is allowed on this machine (some clusters disable it on login nodes). If it isn't allowed, tell me, and remind me to say "sync notes" when I finish writing.

## 6. Test and report
- Touch-test: add a line `<!-- sync test -->` to the end of `progress/Progress.md` (or the main note if it's named differently), run the sync, check it reached GitHub (`git -C $NOTES_DIR log -1 --stat`), then remove the line and sync again.
- Report briefly:
  - `NOTES_DIR` and which folder you linked, and why it matched
  - the `git status` / `check-ignore` proof that `progress/` is invisible to git
  - which rule and hooks you installed, and whether cron is on
  - anything you skipped, and anything I need to do (e.g. add the deploy key)
