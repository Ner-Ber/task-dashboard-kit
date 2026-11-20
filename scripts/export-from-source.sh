#!/usr/bin/env bash
# Refresh the kit from the maintainer's own setup (READ-ONLY on the source).
#
#   scripts/export-from-source.sh <infra-clone> [<notes-clone>]
#
# <infra-clone>  local clone of the source dashboard/tooling repo
# <notes-clone>  optional: local clone of the source notes repo (only its .notes/ tooling is read)
#
# Copies an explicit ALLOWLIST of app/tooling files, runs each through
# scripts/generalize.py (personal categories, names and repos → config/placeholders),
# then runs scripts/privacy-check.sh on the whole kit. Vault or notes CONTENT is never
# read or copied. Nothing is committed: review `git diff` and commit yourself.
set -euo pipefail
KIT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SRC="${1:?usage: export-from-source.sh <infra-clone> [<notes-clone>]}"
NOTES_SRC="${2:-}"
SRC="$(cd "$SRC" && pwd)"
G="python3 $KIT/scripts/generalize.py"

[[ -f "$SRC/skills/dashboard/dashboard.py" ]] || { echo "not an infra clone: $SRC" >&2; exit 1; }

# ---- allowlist: <mode> <source path relative to the infra clone> <kit path>
$G dashboard      "$SRC/skills/dashboard/dashboard.py"           "$KIT/app/dashboard.py"
$G open-dashboard "$SRC/scripts/open-dashboard.sh"               "$KIT/scripts/open-dashboard.sh"
$G shortcuts      "$SRC/scripts/install-dashboard-shortcuts.sh"  "$KIT/scripts/install-windows-shortcuts.sh"
$G test-notes     "$SRC/tests/test_notes.py"                     "$KIT/tests/test_notes.py"

# Static assets: the Notes page, icons, vendored JS (text files are privacy-checked).
rm -rf "$KIT/app/static"
mkdir -p "$KIT/app/static"
( cd "$SRC/skills/dashboard/static" && find . -type f \
    \( -path './notes/*' -o -path './vendor/*' -o -name 'favicon.ico' -o -name 'icon.svg' -o -name 'app.ico' \) -print0 ) |
while IFS= read -r -d '' f; do
  f="${f#./}"
  case "$f" in
    notes/notes.css) $G notes-css "$SRC/skills/dashboard/static/$f" "$KIT/app/static/$f" >/dev/null ;;
    notes/index.html) $G notes-html "$SRC/skills/dashboard/static/$f" "$KIT/app/static/$f" >/dev/null ;;
    notes/notes.js)  $G notes-js  "$SRC/skills/dashboard/static/$f" "$KIT/app/static/$f" >/dev/null ;;
    *.js|*.css|*.html|*.md|*.svg) $G plain "$SRC/skills/dashboard/static/$f" "$KIT/app/static/$f" >/dev/null ;;
    *) mkdir -p "$(dirname "$KIT/app/static/$f")"; cp "$SRC/skills/dashboard/static/$f" "$KIT/app/static/$f" ;;
  esac
done
echo "generalize: static         $KIT/app/static ($(find "$KIT/app/static" -type f | wc -l) files)"

# Notes-repo tooling (never notes content).
if [[ -n "$NOTES_SRC" ]]; then
  $G plain        "$NOTES_SRC/.notes/sync.sh"                 "$KIT/templates/notes/.notes/sync.sh"
  $G agent-prompt "$NOTES_SRC/.notes/cursor-setup-prompt.md"  "$KIT/templates/notes/.notes/agent-setup-prompt.md"
fi

"$KIT/scripts/privacy-check.sh"
echo "Exported from $SRC @ $(git -C "$SRC" rev-parse --short HEAD). Review: git -C \"$KIT\" diff --stat"
