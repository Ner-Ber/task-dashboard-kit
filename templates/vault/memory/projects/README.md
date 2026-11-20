# Project files

One file per project, named like its task-list folder (`tasks/<category>/<slug>/` →
`memory/projects/<slug>.md`). The first `# ` heading is the display name. Optional
fields, one per line:

- `**Domain:** Work` — category label (else taken from the folder)
- `**Status:** active | paused | submitted | done` — done/paused hide the project card
- `**Repo:** \`owner/name\`` or `\`owner/name@branch\`` — code repos (several allowed);
  the card shows their commits / PRs and code TODO files
- `**Related repos:** \`owner/name\`` — shown as links only
- `**Local:** \`~/code/name\`` — where the code lives on disk (text only)
- `## Next action` — one line the card shows when nothing else is set
