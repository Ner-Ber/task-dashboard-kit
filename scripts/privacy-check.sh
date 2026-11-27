#!/usr/bin/env bash
# Scan the kit for things that must never be published.
#
#   scripts/privacy-check.sh [path]        (default: the kit root)
#
# 1. Built-in patterns: tokens/keys (GitHub, Anthropic, OpenAI, AWS, Slack, Google),
#    private-key blocks, e-mail addresses, absolute home paths.
# 2. Your PRIVATE denylist (names, project names, repos, machines): one extended regex per
#    line in $KIT_DENYLIST, default ~/.config/task-dashboard-kit/denylist.txt. Keep that file
#    out of every repo: it contains exactly what it guards.
# 3. gitleaks / trufflehog, if installed.
# Exit 1 on any hit.
set -uo pipefail
KIT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ROOT="${1:-$KIT}"
DENY="${KIT_DENYLIST:-$HOME/.config/task-dashboard-kit/denylist.txt}"
fail=0

# Text files only; vendored minified libraries are third-party and skipped for e-mail/path checks.
mapfile -d '' FILES < <(cd "$ROOT" && find . -type f -not -path './.git/*' -not -name '*.png' -not -name '*.webp' \
  -not -name '*.ico' -not -name '*.woff2' -not -name '*.jpg' -print0)

scan() {  # scan <label> <regex> [exclude-regex-for-paths] [ignore-regex-for-matched-lines] [strip-regex]
  # strip-regex: text removed from a matched line before re-checking it (e.g. the kit's own URL).
  local label="$1" rx="$2" skip="${3:-^$}" ignore="${4:-^$}" strip="${5:-}" hits
  hits="$(cd "$ROOT" && for f in "${FILES[@]}"; do [[ "$f" =~ $skip ]] || printf '%s\0' "$f"; done |
          xargs -0 -r grep -nIE -- "$rx" 2>/dev/null | grep -vE -- "$ignore" |
          { if [[ -n "$strip" ]]; then sed -E "s#$strip##g" | grep -E -- "$rx"; else cat; fi; } | head -20)"
  if [[ -n "$hits" ]]; then
    echo "privacy-check: $label"; echo "$hits" | sed 's/^/  /'; fail=1
  fi
}

scan "possible token / key" \
  '(gh[pousr]_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{40,}|sk-ant-[A-Za-z0-9_-]{20,}|sk-[A-Za-z0-9]{32,}|AKIA[0-9A-Z]{16}|xox[abpr]-[A-Za-z0-9-]{10,}|AIza[0-9A-Za-z_-]{35}|-----BEGIN [A-Z ]*PRIVATE KEY-----)'
scan "e-mail address" '[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.(com|org|net|edu|io|il|de|uk|ac)\b' '(^\./app/static/vendor/|privacy-check\.sh$)' \
  '(git@github\.com|noreply@|@example\.(com|org))'
scan "absolute home path" '(/home/[a-z][a-z0-9_-]*/|/Users/[A-Za-z][^/ ]*/|[A-Z]:\\Users\\[^\\ ]+\\)' '(^\./app/static/vendor/|privacy-check\.sh$)' \
  '(/home/you/|/Users/you/|Users\\you\\|/home/runner/)'

# The kit's own public URL (from `git remote`) may name its owner; that is not a leak.
SELF="$(git -C "$KIT" remote get-url origin 2>/dev/null | sed -E 's#^git@github\.com:#github.com/#; s#^https?://##; s#\.git$##')"
SELF_RX="$(printf '%s' "$SELF" | sed -E 's/[].[^$*+?(){}|\\/]/\\&/g')"

if [[ -f "$DENY" ]]; then
  n=0
  while IFS= read -r rx; do
    [[ -z "$rx" || "$rx" == \#* ]] && continue
    n=$((n + 1))
    scan "denylist match (pattern #$n)" "$rx" '^\./app/static/vendor/' '^$' "$SELF_RX"
  done < "$DENY"
  echo "privacy-check: $n private denylist patterns checked ($DENY)"
else
  echo "privacy-check: no private denylist at $DENY (built-in patterns only)"
fi

if command -v gitleaks >/dev/null; then
  gitleaks detect --no-git --source "$ROOT" --redact -q || fail=1
elif command -v trufflehog >/dev/null; then
  trufflehog filesystem "$ROOT" --no-update --fail -j >/dev/null || fail=1
else
  echo "privacy-check: gitleaks/trufflehog not installed (skipped)"
fi

if (( fail )); then echo "privacy-check: FAILED"; exit 1; fi
echo "privacy-check: clean"
