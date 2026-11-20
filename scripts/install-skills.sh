#!/usr/bin/env bash
# Copy the kit's Agent Skills (skills/*/SKILL.md) to where coding agents look for them.
#
#   scripts/install-skills.sh <repo-dir>     project scope: <repo-dir>/.agents/skills (Codex, Cursor,
#                                            Gemini CLI, Copilot in VS Code) + <repo-dir>/.claude/skills
#   scripts/install-skills.sh --user         user scope: ~/.agents/skills, ~/.claude/skills,
#                                            ~/.cursor/skills, ~/.copilot/skills, ~/.gemini/skills
#   add --github to also write <repo-dir>/.github/skills (GitHub Copilot cloud agent)
#
# Re-run after updating the kit; existing copies of these skills are replaced.
set -euo pipefail
KIT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
TARGETS=(); GITHUB=0; DIR=""
for a in "$@"; do
  case "$a" in
    --user) TARGETS+=("$HOME/.agents/skills" "$HOME/.claude/skills" "$HOME/.cursor/skills" "$HOME/.copilot/skills" "$HOME/.gemini/skills") ;;
    --github) GITHUB=1 ;;
    -h|--help) sed -n '2,11p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) DIR="$a" ;;
  esac
done
if [[ -n "$DIR" ]]; then
  DIR="$(cd "$DIR" && pwd)"
  TARGETS+=("$DIR/.agents/skills" "$DIR/.claude/skills")
  (( GITHUB )) && TARGETS+=("$DIR/.github/skills")
fi
(( ${#TARGETS[@]} )) || { sed -n '2,11p' "$0" | sed 's/^# \{0,1\}//'; exit 2; }
for t in "${TARGETS[@]}"; do
  mkdir -p "$t"
  for s in "$KIT"/skills/*/; do
    name="$(basename "$s")"
    rm -rf "${t:?}/$name"
    cp -R "$s" "$t/$name"
  done
  echo "skills → $t ($(ls "$KIT/skills" | tr '\n' ' '))"
done
