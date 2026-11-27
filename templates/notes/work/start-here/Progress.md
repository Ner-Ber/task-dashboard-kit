# Progress

## {{h+0}} - Welcome

This is a **progress note**: a lab diary for one project. Write what you did, what you found and what's next, so future you (and your coding agent) can pick up where you left off.

- **Your turn:** add today's entry above this one. Click into the editor and type; it saves by itself 2 s after you stop and syncs with git in the background.
- Entries are dated headings like `## {{h+0}}`, **newest on top**.
- Paste a screenshot with Ctrl+V: it lands in this folder's `attachments/`.
- Math works too: $E = mc^2$, and on its own line $$\int_0^1 x\,dx = \tfrac12$$

## {{h-1}} - Notes belong to projects

- This note lives in your **notes repo** under `work/start-here/`. Its tasks live in your **vault** under `tasks/work/start-here/`. Same folder names, two private repos.
- In a code project, your agent can link that project's notes folder in as `progress/` (`scripts/link-notes.sh work/website ~/code/website`). Your editor and agent see the notes there, but git never does: nothing gets committed, even in a public repo.

## {{h-2}} - When the same note changed in two places

Edited a note on two machines at once? Nothing is lost: the other version is saved next to the note as a **conflict copy** (shown in red in the list). There's one here to practise on. Open it, look at the diff, press **⇄ Merge**. Entries that only the copy has are added; for an entry both have, the newer edit wins.
