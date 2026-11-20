#!/usr/bin/env bash
# Install Windows Desktop + Start Menu (+ optional Startup) shortcuts for the
# live tasks dashboard. Runs open-dashboard via wsl.exe with no visible CMD
# (VBScript Run …, 0). Bind stays 127.0.0.1 only — never 0.0.0.0.
#
# Idempotent: re-running refreshes launcher scripts and .lnk targets.
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
OPEN_SH="$INFRA/scripts/open-dashboard.sh"
SHORTCUT_NAME="${DASHBOARD_SHORTCUT_NAME:-Tasks}"
# shell32 checklist-ish glyph (works on stock Windows).
# Default icon: app/static/app.ico, copied next to the launchers (set below).
ICON_LOCATION="${DASHBOARD_ICON_LOCATION:-}"

DRY_RUN=0
WITH_AUTOSTART=1
NO_AUTOSTART=0
usage() {
  cat <<'EOF'
Usage: install-dashboard-shortcuts.sh [--dry-run] [--no-autostart] [--with-autostart]

  Creates (or refreshes) Windows shortcuts named "Tasks" that open the
  live dashboard at http://127.0.0.1:8765/ with no CMD window.

  --dry-run         Print planned paths and VBS/lnk targets; write nothing.
  --with-autostart  Also install a Startup-folder shortcut that starts serve
                    quietly (--if-needed --no-browser). Default on.
  --no-autostart    Skip the Startup shortcut (Desktop + Start Menu only).

Requires WSL with /mnt/c and powershell.exe / wscript on the Windows side.
EOF
}

for arg in "$@"; do
  case "$arg" in
    --dry-run) DRY_RUN=1 ;;
    --with-autostart) WITH_AUTOSTART=1; NO_AUTOSTART=0 ;;
    --no-autostart) NO_AUTOSTART=1; WITH_AUTOSTART=0 ;;
    -h|--help) usage; exit 0 ;;
    *)
      echo "Unknown option: $arg" >&2
      usage >&2
      exit 2
      ;;
  esac
done

if [[ ! -f "$OPEN_SH" ]]; then
  echo "open-dashboard.sh not found at $OPEN_SH (set ASSISTANT_INFRA)." >&2
  exit 1
fi
if [[ ! -d /mnt/c/Users ]]; then
  echo " /mnt/c/Users not found — this installer is for Windows + WSL only." >&2
  exit 1
fi
if ! command -v powershell.exe >/dev/null 2>&1; then
  echo "powershell.exe not on PATH (need Windows host from WSL)." >&2
  exit 1
fi

# --- Discover Windows folders (never hardcode a wrong username) -------------
_ps_folder() {
  # $1 = .NET SpecialFolder name or GetFolderPath argument
  powershell.exe -NoProfile -Command "[Environment]::GetFolderPath('$1')" \
    | tr -d '\r' | tail -n1
}

_win_to_mnt() {
  # C:\Users\you\Desktop → /mnt/c/Users/you/Desktop
  local win="$1"
  win="${win//$'\r'/}"
  win="${win//\\//}"
  if [[ "$win" =~ ^([A-Za-z]):/(.*)$ ]]; then
    local drive="${BASH_REMATCH[1],,}"
    echo "/mnt/${drive}/${BASH_REMATCH[2]}"
  else
    echo "Cannot convert Windows path: $1" >&2
    return 1
  fi
}

_mnt_to_win() {
  # /mnt/c/Users/you/x → C:\Users\you\x
  local mnt="$1"
  if [[ "$mnt" =~ ^/mnt/([a-zA-Z])/(.*)$ ]]; then
    local drive="${BASH_REMATCH[1]^^}"
    local rest="${BASH_REMATCH[2]//\//\\}"
    echo "${drive}:\\${rest}"
  else
    echo "Cannot convert WSL path: $1" >&2
    return 1
  fi
}

WIN_USER="$(powershell.exe -NoProfile -Command '$env:USERNAME' | tr -d '\r' | tail -n1)"
if [[ -z "$WIN_USER" || "$WIN_USER" == "defaultuser0" ]]; then
  # Fallback: only real profile dirs under /mnt/c/Users (skip Public/Default).
  WIN_USER="$(
    find /mnt/c/Users -mindepth 1 -maxdepth 1 -type d ! -name Public ! -name Default \
      ! -name 'Default User' ! -name 'All Users' -printf '%f\n' 2>/dev/null \
      | head -n1
  )"
fi
if [[ -z "$WIN_USER" ]]; then
  echo "Could not detect Windows username." >&2
  exit 1
fi

WIN_DESKTOP="$(_ps_folder Desktop)"
WIN_PROGRAMS="$(_ps_folder Programs)"
WIN_STARTUP="$(_ps_folder Startup)"
WIN_LOCALAPPDATA="$(powershell.exe -NoProfile -Command '$env:LOCALAPPDATA' | tr -d '\r' | tail -n1)"

if [[ -z "$WIN_DESKTOP" || -z "$WIN_PROGRAMS" || -z "$WIN_LOCALAPPDATA" ]]; then
  echo "Could not resolve Desktop/Programs/LocalAppData via PowerShell." >&2
  exit 1
fi

MNT_DESKTOP="$(_win_to_mnt "$WIN_DESKTOP")"
MNT_PROGRAMS="$(_win_to_mnt "$WIN_PROGRAMS")"
MNT_STARTUP="$(_win_to_mnt "$WIN_STARTUP")"
MNT_APPDATA="$(_win_to_mnt "$WIN_LOCALAPPDATA")"
LAUNCHER_DIR="$MNT_APPDATA/Assistant/dashboard"
WIN_LAUNCHER_DIR="$(_mnt_to_win "$LAUNCHER_DIR")"

APP_ICO_SRC="$INFRA/app/static/app.ico"
if [[ -z "$ICON_LOCATION" ]]; then
  if [[ -f "$APP_ICO_SRC" ]]; then
    ICON_LOCATION="$WIN_LAUNCHER_DIR\\tasks.ico,0"
  else
    ICON_LOCATION='%SystemRoot%\System32\shell32.dll,71'
  fi
fi
OPEN_VBS="$LAUNCHER_DIR/open-tasks.vbs"
SERVE_VBS="$LAUNCHER_DIR/serve-tasks.vbs"
DESKTOP_LNK="$MNT_DESKTOP/${SHORTCUT_NAME}.lnk"
START_LNK="$MNT_PROGRAMS/${SHORTCUT_NAME}.lnk"
STARTUP_LNK="$MNT_STARTUP/${SHORTCUT_NAME} serve.lnk"

# Paths go into bash single quotes inside a VBS double-quoted -lc string.
for _p in "$VAULT" "$INFRA" "$OPEN_SH"; do
  if [[ "$_p" == *\'* ]]; then
    echo "Path contains a single quote (unsupported): $_p" >&2
    exit 1
  fi
done

# No double quotes in the -lc payload (VBS uses "" for one "). Prefer default WSL
# distro. Source bashrc first so ASSISTANT_* from ~/.bashrc win; baked defaults
# cover a missing bashrc. Paths have no spaces on this machine.
OPEN_BASH_CMD="source \$HOME/.bashrc 2>/dev/null || true; export ASSISTANT_VAULT=\${ASSISTANT_VAULT:-$VAULT} ASSISTANT_INFRA=\${ASSISTANT_INFRA:-$INFRA}; exec \$ASSISTANT_INFRA/scripts/open-dashboard.sh"
SERVE_BASH_CMD="source \$HOME/.bashrc 2>/dev/null || true; export ASSISTANT_VAULT=\${ASSISTANT_VAULT:-$VAULT} ASSISTANT_INFRA=\${ASSISTANT_INFRA:-$INFRA}; exec \$ASSISTANT_INFRA/scripts/open-dashboard.sh --if-needed --no-browser"

_make_vbs() {
  local out="$1" bash_inner="$2" label="$3"
  # WindowStyle 0 = hidden: no CMD flash. False = do not wait (serve keeps running).
  cat >"$out" <<EOF
' ${label} - generated by install-dashboard-shortcuts.sh (do not edit by hand).
' Runs WSL open-dashboard with no visible console. Loopback-only serve.
Option Explicit
Dim sh, cmd
Set sh = CreateObject("WScript.Shell")
cmd = "wsl.exe -e bash -lc ""${bash_inner}"""
sh.Run cmd, 0, False
EOF
}

_make_lnk() {
  local win_lnk="$1" win_vbs="$2" desc="$3"
  # wscript.exe //B = batch mode (no script UI). .lnk WindowStyle 7 = minimized.
  powershell.exe -NoProfile -Command "
\$ws = New-Object -ComObject WScript.Shell
\$s = \$ws.CreateShortcut('${win_lnk//\'/\'\'}')
\$s.TargetPath = 'wscript.exe'
\$s.Arguments = '//B \"${win_vbs//\'/\'\'}\"'
\$s.WorkingDirectory = '%USERPROFILE%'
\$s.WindowStyle = 7
\$s.Description = '${desc//\'/\'\'}'
\$s.IconLocation = '${ICON_LOCATION}'
\$s.Save()
" >/dev/null
}

echo "Windows user:  $WIN_USER"
echo "Desktop:       $WIN_DESKTOP"
echo "Start Menu:    $WIN_PROGRAMS"
echo "Startup:       $WIN_STARTUP"
echo "Launchers:     $WIN_LAUNCHER_DIR"
echo "Infra script:  $OPEN_SH"
echo "URL:           $URL"
echo "Autostart:     $([[ "$WITH_AUTOSTART" -eq 1 ]] && echo yes || echo no)"

if [[ "$DRY_RUN" -eq 1 ]]; then
  echo
  echo "[dry-run] would write:"
  echo "  $OPEN_VBS"
  echo "  $DESKTOP_LNK"
  echo "  $START_LNK"
  if [[ "$WITH_AUTOSTART" -eq 1 ]]; then
    echo "  $SERVE_VBS"
    echo "  $STARTUP_LNK"
  fi
  echo
  echo "[dry-run] open VBS would Run (hidden):"
  echo "  wsl.exe -e bash -lc \"${OPEN_BASH_CMD}\""
  if [[ "$WITH_AUTOSTART" -eq 1 ]]; then
    echo "[dry-run] serve VBS would Run (hidden):"
    echo "  wsl.exe -e bash -lc \"${SERVE_BASH_CMD}\""
  fi
  exit 0
fi

mkdir -p "$LAUNCHER_DIR"
[[ -f "$APP_ICO_SRC" ]] && cp "$APP_ICO_SRC" "$LAUNCHER_DIR/tasks.ico"
_make_vbs "$OPEN_VBS" "$OPEN_BASH_CMD" "Assistant Tasks — open dashboard"
WIN_OPEN_VBS="$(_mnt_to_win "$OPEN_VBS")"
_make_lnk "$(_mnt_to_win "$DESKTOP_LNK")" "$WIN_OPEN_VBS" "Assistant Tasks dashboard (http://127.0.0.1:${PORT}/)"
_make_lnk "$(_mnt_to_win "$START_LNK")" "$WIN_OPEN_VBS" "Assistant Tasks dashboard (http://127.0.0.1:${PORT}/)"

if [[ "$WITH_AUTOSTART" -eq 1 ]]; then
  _make_vbs "$SERVE_VBS" "$SERVE_BASH_CMD" "Assistant Tasks — quiet serve (autostart)"
  WIN_SERVE_VBS="$(_mnt_to_win "$SERVE_VBS")"
  _make_lnk "$(_mnt_to_win "$STARTUP_LNK")" "$WIN_SERVE_VBS" \
    "Start Assistant Tasks serve on login (127.0.0.1:${PORT}, no browser)"
fi

# Optional user-systemd unit (no-op install if bus missing; file still written).
UNIT_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"
UNIT_FILE="$UNIT_DIR/assistant-dashboard.service"
mkdir -p "$UNIT_DIR"
cat >"$UNIT_FILE" <<EOF
[Unit]
Description=Assistant Tasks dashboard (127.0.0.1:${PORT})
After=network.target
# Personal machine / WSL only — never on a shared server.

[Service]
Type=simple
Environment=ASSISTANT_VAULT=${VAULT}
Environment=ASSISTANT_INFRA=${INFRA}
Environment=DASHBOARD_PORT=${PORT}
ExecStart=${OPEN_SH} --if-needed --no-browser
Restart=on-failure
RestartSec=5

[Install]
WantedBy=default.target
EOF

if command -v systemctl >/dev/null 2>&1 && systemctl --user status >/dev/null 2>&1; then
  systemctl --user daemon-reload
  if [[ "$WITH_AUTOSTART" -eq 1 ]]; then
    systemctl --user enable --now assistant-dashboard.service >/dev/null 2>&1 || true
    echo "systemd user unit: enabled ($UNIT_FILE)"
  else
    systemctl --user disable --now assistant-dashboard.service >/dev/null 2>&1 || true
    echo "systemd user unit: written but not enabled ($UNIT_FILE)"
  fi
else
  echo "systemd user bus unavailable — relying on Windows Startup shortcut (unit file at $UNIT_FILE for later)."
fi

echo
echo "Installed:"
echo "  Desktop:    $DESKTOP_LNK"
echo "  Start Menu: $START_LNK"
if [[ "$WITH_AUTOSTART" -eq 1 ]]; then
  echo "  Startup:    $STARTUP_LNK"
fi
echo
echo "Daily open: click \"${SHORTCUT_NAME}\" on the Desktop (or Start Menu), or bookmark ${URL} after login when autostart is on."
echo "Never bind 0.0.0.0; Android later = private tunnel/auth to this same host."
