#!/usr/bin/env bash
# Sync this notes repo: commit local edits, pull --rebase, push.
# Same conflict policy as the dashboard: never lose text. If a note was changed
# both here and elsewhere, keep THIS machine's version in the note and save the
# other version beside it as "<note>.conflict-<timestamp>.md".
#   .notes/sync.sh            sync once (quiet; prints only problems and conflicts)
#   .notes/sync.sh --pull     only pull (when there are no local edits)
# Safe to run often and concurrently (flock); exits 0 when there is nothing to do.
set -uo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT" || exit 1
GITDIR="$(git rev-parse --git-dir)" || exit 1
exec 9>"$GITDIR/notes-sync.lock"
flock -w 20 9 || { echo "notes-sync: another sync is running" >&2; exit 0; }

if [[ "${1:-}" == "--pull" ]]; then
  [[ -z "$(git status --porcelain)" ]] && git pull -q --rebase
  exit 0
fi

git add -A
if ! git diff --cached --quiet; then
  n="$(git diff --cached --name-only | grep -c '\.md$')"
  first="$(git diff --cached --name-only | grep '\.md$' | head -1 | sed 's/\.md$//')"
  msg="notes: ${first:-attachments}"
  (( n > 1 )) && msg="$msg (+$((n - 1)) more)"
  git commit -q -m "$msg ($(hostname -s))"
fi

if ! git pull -q --rebase 2>/dev/null; then
  mapfile -d '' -t conflicted < <(git diff -z --name-only --diff-filter=U)
  git rebase --abort 2>/dev/null
  if ! git merge -q --no-edit -X ours '@{u}'; then
    git merge --abort 2>/dev/null
    echo "notes-sync: could not merge automatically; nothing was lost, resolve by hand in $ROOT" >&2
    exit 1
  fi
  ts="$(date +%Y%m%d-%H%M%S)"
  copies=()
  for f in "${conflicted[@]}"; do
    [[ "$f" == *.md ]] || continue
    copy="${f%.md}.conflict-$ts.md"
    git show "@{u}:$f" > "$copy" && copies+=("$copy")
  done
  if (( ${#copies[@]} )); then
    git add -- "${copies[@]}"
    git commit -q -m "notes: conflict copy ${copies[0]} ($(hostname -s))"
    printf 'notes-sync: CONFLICT - your text kept; other version saved as: %s\n' "${copies[@]}" >&2
  fi
fi
git push -q 2>/dev/null || { git pull -q --rebase && git push -q; } || echo "notes-sync: push failed (offline?) - will retry next sync" >&2
