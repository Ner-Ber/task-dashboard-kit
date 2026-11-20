#!/usr/bin/env bash
# Show a project's private progress notes inside a code repository, without git ever
# seeing them.
#
#   scripts/link-notes.sh <project-folder> <code-repo> [--name progress]
#   scripts/link-notes.sh --unlink <code-repo> [--name progress]
#
# <project-folder>  the project's folder in your notes repo, either relative to
#                   $ASSISTANT_NOTES (e.g. work/website) or a path to it
# <code-repo>       any folder inside the code repository (a linked worktree works too)
#
# Creates <code-repo-top>/progress -> <notes>/<project-folder> (a symlink) and adds
# "/progress" to the repo's .git/info/exclude: a LOCAL ignore list that is never
# committed, so the repo (even a public one) stays unchanged in git. Idempotent.
# Refuses if "progress" is tracked by git or is already something else.
# Works on Linux, macOS and WSL. Native Windows: scripts/link-notes.ps1.
set -euo pipefail

usage() { sed -n '2,17p' "$0" | sed 's/^# \{0,1\}//'; exit "${1:-0}"; }
KIT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
NAME="progress"; UNLINK=0; ARGS=()
while (( $# )); do
  case "$1" in
    --unlink) UNLINK=1 ;;
    --name) NAME="${2:?--name needs a value}"; shift ;;
    -h|--help) usage 0 ;;
    *) ARGS+=("$1") ;;
  esac
  shift
done
[[ "$NAME" =~ ^[A-Za-z0-9._-]+$ && "$NAME" != "." && "$NAME" != ".." ]] || { echo "bad --name: $NAME" >&2; exit 2; }

# ASSISTANT_NOTES from the environment or the kit's .env.
if [[ -z "${ASSISTANT_NOTES:-}" && -f "$KIT/.env" ]]; then
  v="$(sed -nE 's/^[[:space:]]*(export[[:space:]]+)?ASSISTANT_NOTES=(.*)$/\2/p' "$KIT/.env" | tail -1)"
  v="${v%\"}"; v="${v#\"}"; v="${v/#\~/$HOME}"; v="${v//\$HOME/$HOME}"
  [[ -n "$v" ]] && ASSISTANT_NOTES="$v"
fi

if (( UNLINK )); then
  (( ${#ARGS[@]} == 1 )) || usage 2
  CODE="${ARGS[0]}"
else
  (( ${#ARGS[@]} == 2 )) || usage 2
  PROJ="${ARGS[0]}"; CODE="${ARGS[1]}"
fi

TOP="$(git -C "$CODE" rev-parse --show-toplevel 2>/dev/null)" || { echo "not a git repository: $CODE" >&2; exit 1; }
LINK="$TOP/$NAME"
EXCLUDE="$(git -C "$TOP" rev-parse --git-path info/exclude)"
[[ "$EXCLUDE" = /* ]] || EXCLUDE="$TOP/$EXCLUDE"
ENTRY="/$NAME"

if git -C "$TOP" ls-files --error-unmatch -- "$NAME" >/dev/null 2>&1 || \
   [[ -n "$(git -C "$TOP" ls-files -- "$NAME/")" ]]; then
  echo "refusing: '$NAME' is tracked by git in $TOP; pick another --name" >&2; exit 1
fi

if (( UNLINK )); then
  if [[ -L "$LINK" ]]; then rm "$LINK"; echo "removed link $LINK"
  elif [[ -e "$LINK" ]]; then echo "refusing: $LINK is not a symlink; left alone" >&2; exit 1
  else echo "no link at $LINK"; fi
  if [[ -f "$EXCLUDE" ]] && grep -qxF "$ENTRY" "$EXCLUDE"; then
    tmp="$(mktemp)"
    grep -vxF "$ENTRY" "$EXCLUDE" | grep -vxF "# private progress notes (link-notes)" > "$tmp" || true
    cat "$tmp" > "$EXCLUDE"; rm -f "$tmp"
    echo "removed $ENTRY from $EXCLUDE"
  fi
  exit 0
fi

# Resolve the notes folder.
if [[ -d "$PROJ" ]]; then TARGET="$(cd "$PROJ" && pwd)"
elif [[ -n "${ASSISTANT_NOTES:-}" && -d "$ASSISTANT_NOTES/$PROJ" ]]; then TARGET="$(cd "$ASSISTANT_NOTES/$PROJ" && pwd)"
else
  echo "notes folder not found: $PROJ (relative to \$ASSISTANT_NOTES=${ASSISTANT_NOTES:-unset}, or a path)" >&2; exit 1
fi
case "$TARGET/" in "$TOP/"*) echo "refusing: the notes folder is inside the code repo" >&2; exit 1 ;; esac

if [[ -L "$LINK" ]]; then
  if [[ "$(cd "$LINK" 2>/dev/null && pwd -P)" == "$(cd "$TARGET" && pwd -P)" ]]; then
    echo "link already in place: $LINK -> $TARGET"
  else
    echo "refusing: $LINK already links to $(readlink "$LINK"); run --unlink first" >&2; exit 1
  fi
elif [[ -e "$LINK" ]]; then
  echo "refusing: $LINK exists and is not a link; move it first" >&2; exit 1
else
  ln -s "$TARGET" "$LINK"
  echo "linked $LINK -> $TARGET"
fi

mkdir -p "$(dirname "$EXCLUDE")"
if ! { [[ -f "$EXCLUDE" ]] && grep -qxF "$ENTRY" "$EXCLUDE"; }; then
  { [[ -s "$EXCLUDE" && -n "$(tail -c1 "$EXCLUDE")" ]] && echo; echo "# private progress notes (link-notes)"; echo "$ENTRY"; } >> "$EXCLUDE"
  echo "added $ENTRY to $EXCLUDE (local only, never committed)"
fi

seen="$(git -C "$TOP" status --porcelain --untracked-files=all -- "$NAME")"
if [[ -n "$seen" ]]; then echo "ERROR: git still sees $NAME:" >&2; echo "$seen" >&2; exit 1; fi
echo "verified: git status in $TOP does not show $NAME/"
