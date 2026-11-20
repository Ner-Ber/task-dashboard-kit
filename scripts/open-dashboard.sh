#!/usr/bin/env bash
# Open the live dashboard in the browser (Linux / macOS / WSL).
# Binds 127.0.0.1 only — never expose beyond the local machine.
#
# Click path (default): if something already listens on the port, open the browser
# immediately and refresh the vault in the background (no long pull before paint).
# Cold start: start serve first; pull/collect run in the background.
# Use --restart to force kill + fresh serve (after infra updates).
# Auto-update: every launch fast-forwards the infra checkout to origin/main (only
# when it is on main with no local changes); if code changed, serve restarts so
# merged PRs go live without a terminal. DASHBOARD_NO_AUTO_UPDATE=1 turns it off.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
INFRA="${ASSISTANT_INFRA:-$(cd "$SCRIPT_DIR/.." && pwd)}"
# Settings from the kit's .env (see .env.example); the real environment wins.
if [[ -f "$INFRA/.env" ]]; then
  while IFS= read -r _l; do
    [[ "$_l" =~ ^[[:space:]]*(export[[:space:]]+)?([A-Za-z_][A-Za-z0-9_]*)=(.*)$ ]] || continue
    _k="${BASH_REMATCH[2]}"; _v="${BASH_REMATCH[3]}"; _v="${_v%\"}"; _v="${_v#\"}"
    _v="${_v/#\~/$HOME}"; _v="${_v//\$HOME/$HOME}"
    [[ -n "${!_k+x}" ]] || export "$_k=$_v"
  done < "$INFRA/.env"
fi
VAULT="${ASSISTANT_VAULT:-$HOME/assistant}"
PORT="${DASHBOARD_PORT:-8765}"
URL="http://127.0.0.1:${PORT}/"
DPY="$INFRA/app/dashboard.py"

# Listeners on TCP $1 (LISTEN). Prefer ss; fall back to lsof. Empty if none/unknown.
_dashboard_listen_pids() {
  local port="$1" pids=""
  if command -v ss >/dev/null 2>&1; then
    pids=$(ss -ltnp "sport = :${port}" 2>/dev/null \
      | sed -n 's/.*pid=\([0-9][0-9]*\).*/\1/p' | sort -u | tr '\n' ' ')
  fi
  if [[ -z "${pids// }" ]] && command -v lsof >/dev/null 2>&1; then
    pids=$(lsof -t -nP -iTCP:"${port}" -sTCP:LISTEN 2>/dev/null | sort -u | tr '\n' ' ' || true)
  fi
  echo "${pids}"
}

# True if something is still listening on TCP $1.
_dashboard_port_busy() {
  local port="$1"
  if command -v ss >/dev/null 2>&1; then
    ss -ltn "sport = :${port}" 2>/dev/null | grep -q ":${port}" && return 0
    return 1
  fi
  if command -v lsof >/dev/null 2>&1; then
    lsof -nP -iTCP:"${port}" -sTCP:LISTEN >/dev/null 2>&1 && return 0
    return 1
  fi
  return 1
}

# Graceful then force-kill listeners on $1; wait until the port is free.
free_dashboard_port() {
  local port="$1" pids i
  pids="$(_dashboard_listen_pids "$port")"
  if [[ -z "${pids// }" ]]; then
    if _dashboard_port_busy "$port"; then
      echo "Port $port is busy but no PID found (need ss/lsof); cannot restart." >&2
      return 1
    fi
    return 0
  fi
  echo "Port $port in use (pids:${pids}) — stopping for restart…" >&2
  # shellcheck disable=SC2086
  kill $pids 2>/dev/null || true
  for i in $(seq 1 25); do
    _dashboard_port_busy "$port" || return 0
    sleep 0.1
  done
  echo "Port $port still busy — force killing…" >&2
  # shellcheck disable=SC2086
  kill -9 $pids 2>/dev/null || true
  # Re-resolve in case the listener was replaced.
  pids="$(_dashboard_listen_pids "$port")"
  if [[ -n "${pids// }" ]]; then
    # shellcheck disable=SC2086
    kill -9 $pids 2>/dev/null || true
  fi
  for i in $(seq 1 25); do
    _dashboard_port_busy "$port" || return 0
    sleep 0.1
  done
  echo "Could not free port $port" >&2
  return 1
}

# Open $URL in the default browser. Prefer Windows cmd start (fast from WSL);
# wslview alone can take >10s and makes the Tasks icon feel dead.
_open_browser() {
  if command -v cmd.exe >/dev/null 2>&1; then
    # Empty title arg required so start does not treat the URL as a window title.
    cmd.exe /c start "" "$URL" >/dev/null 2>&1 && return 0
  fi
  if command -v powershell.exe >/dev/null 2>&1; then
    powershell.exe -NoProfile -Command "Start-Process '$URL'" >/dev/null 2>&1 && return 0
  fi
  if command -v wslview >/dev/null 2>&1; then
    wslview "$URL" >/dev/null 2>&1 && return 0
  fi
  if command -v xdg-open >/dev/null 2>&1; then
    xdg-open "$URL" >/dev/null 2>&1 && return 0
  fi
  if command -v open >/dev/null 2>&1; then
    open "$URL" >/dev/null 2>&1 && return 0
  fi
  echo "Open in browser: $URL"
}

# Non-blocking vault pull (no credential prompts that hang a hidden VBS launch).
_background_vault_pull() {
  (
    GIT_TERMINAL_PROMPT=0 git -C "$VAULT" pull -q --rebase --autostash \
      || GIT_TERMINAL_PROMPT=0 git -C "$VAULT" pull -q \
      || true
  ) >/dev/null 2>&1 &
}

# Fast-forward $INFRA to origin/main when it is on main and clean. Prints
# "updated" when HEAD moved. Never prompts, never merges, never touches local edits.
_infra_update() {
  [[ "${DASHBOARD_NO_AUTO_UPDATE:-}" == "1" ]] && return 0
  local before after
  [[ "$(git -C "$INFRA" rev-parse --abbrev-ref HEAD 2>/dev/null)" == "main" ]] || return 0
  git -C "$INFRA" diff --quiet 2>/dev/null || return 0
  git -C "$INFRA" diff --cached --quiet 2>/dev/null || return 0
  before="$(git -C "$INFRA" rev-parse HEAD 2>/dev/null)" || return 0
  GIT_TERMINAL_PROMPT=0 timeout "${DASHBOARD_UPDATE_TIMEOUT:-5}" \
    git -C "$INFRA" fetch -q origin main >/dev/null 2>&1 || return 0
  git -C "$INFRA" merge -q --ff-only FETCH_HEAD >/dev/null 2>&1 || return 0
  after="$(git -C "$INFRA" rev-parse HEAD 2>/dev/null)" || return 0
  if [[ "$before" != "$after" ]]; then echo updated; fi
  return 0
}

# Sourced by tests: stop after helpers are defined.
if [[ "${OPEN_DASHBOARD_LIB_ONLY:-}" == "1" ]]; then
  return 0 2>/dev/null || exit 0
fi

NO_BROWSER=0
IF_NEEDED=0
FORCE_RESTART=0
usage() {
  cat <<'EOF'
Usage: open-dashboard.sh [--no-browser] [--if-needed] [--restart]

  (default)     If already serving: open browser now, pull vault in background.
                If not: start serve on 127.0.0.1, open browser; pull in background.
  --no-browser  Do not open a browser (autostart / headless serve).
  --if-needed   If something already listens on the port, exit 0 (no restart).
  --restart     Always free the port and start a fresh serve (after infra updates).
EOF
}
for arg in "$@"; do
  case "$arg" in
    --no-browser) NO_BROWSER=1 ;;
    --if-needed) IF_NEEDED=1 ;;
    --restart) FORCE_RESTART=1 ;;
    -h|--help) usage; exit 0 ;;
    *)
      echo "Unknown option: $arg" >&2
      usage >&2
      exit 2
      ;;
  esac
done

if [[ ! -d "$VAULT/.git" ]]; then
  echo "Vault not found at $VAULT (set ASSISTANT_VAULT)." >&2
  exit 1
fi
if [[ ! -f "$DPY" ]]; then
  echo "dashboard.py not found at $DPY (set ASSISTANT_INFRA)." >&2
  exit 1
fi

# Prefer WSL/personal paths from the Project store when present.
if [[ -f "${CURSOR_PROJECT_STORE:-}/docs/local-env.sh" ]]; then
  # shellcheck disable=SC1090
  source "${CURSOR_PROJECT_STORE}/docs/local-env.sh" || true
  VAULT="${ASSISTANT_VAULT:-$VAULT}"
  INFRA="${ASSISTANT_INFRA:-$INFRA}"
  DPY="$INFRA/app/dashboard.py"
fi

CACHE="${DASHBOARD_CACHE:-$VAULT/dashboard}"
mkdir -p "$CACHE"

# New infra on GitHub (a merged PR) → pull it and restart serve in this launch.
if [[ "$(_infra_update)" == "updated" ]]; then
  echo "Infra updated to $(git -C "$INFRA" log -1 --format='%h %s') — restarting serve."
  FORCE_RESTART=1
  IF_NEEDED=0
fi

if [[ "$IF_NEEDED" -eq 1 ]] && _dashboard_port_busy "$PORT"; then
  echo "Already serving $URL — leaving it alone (--if-needed)."
  if [[ "$NO_BROWSER" -eq 0 ]]; then
    _open_browser
  fi
  exit 0
fi

# Fast path: serve is up → show the page now; pull does not block the click.
if [[ "$FORCE_RESTART" -eq 0 ]] && _dashboard_port_busy "$PORT"; then
  echo "Already serving $URL — opening browser (pull in background)."
  if [[ "$NO_BROWSER" -eq 0 ]]; then
    _open_browser
  fi
  _background_vault_pull
  exit 0
fi

echo "Vault:  $VAULT"
echo "Infra:  $INFRA"

# Cold start / forced restart: do not wait on git pull or collect before bind.
# live_page() re-reads the vault on GET; ↻ Pull / ↻ Update refresh from the UI.
_background_vault_pull
(
  uv run --script "$DPY" collect --vault "$VAULT" -o "$CACHE/data.json" || true
) >/dev/null 2>&1 &

free_dashboard_port "$PORT"

echo "Serving $URL  (Ctrl-C to stop)"
# Open the browser shortly after the server binds.
if [[ "$NO_BROWSER" -eq 0 ]]; then
  (
    sleep 0.4
    _open_browser
  ) >/dev/null 2>&1 &
fi

# Always loopback — never 0.0.0.0 (dashboard.py serve binds 127.0.0.1).
exec uv run --script "$DPY" serve --vault "$VAULT" --cache "$CACHE" --port "$PORT"
