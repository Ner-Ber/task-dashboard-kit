#!/usr/bin/env python3
"""Turn files from the maintainer's personal setup into the kit's generic versions.

    python3 scripts/generalize.py <mode> <source file> <output file>

Modes: dashboard, open-dashboard, shortcuts, test-notes, agent-prompt, plain
(plain = copy unchanged, but still refuse personal strings).

Only personal bits are touched: the hard-coded categories, colours, brand, legacy
folder aliases, and the owner's name/repos in prompts and comments. Everything else is
copied byte for byte, so later improvements to the source port over by re-running
scripts/export-from-source.sh.

Each replacement must match EXACTLY the expected number of times. If the source changed
around one of them, this script stops with an error naming it. Update the rule here,
then re-run; never hand-edit app/dashboard.py.
"""
import os
import re
import sys
from pathlib import Path

# ---------------------------------------------------------------- config block
CONFIG_BLOCK = '''
def _load_dotenv() -> None:
    """Read KEY=VALUE lines from the kit's .env (next to app/) without overriding the
    real environment. Keeps `uv run app/dashboard.py …` working without a wrapper."""
    for f in (Path(__file__).resolve().parent.parent / ".env", Path.cwd() / ".env"):
        if not f.is_file():
            continue
        for line in f.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            k, v = k.strip().removeprefix("export ").strip(), v.strip().strip('"').strip("'")
            v = os.path.expanduser(os.path.expandvars(v))
            os.environ.setdefault(k, v)
        break


_load_dotenv()
DEFAULT_VAULT = Path(os.environ.get("ASSISTANT_VAULT", Path.home() / "assistant")).expanduser()
DASHBOARD_BRAND = os.environ.get("DASHBOARD_BRAND", "Tasks")
ACTIVITY_DAYS = 14
URGENT_DAYS = 2
DESK_DAYS = 14
INACTIVE_STATUSES = {"done", "paused", "submitted"}


def _categories() -> dict:
    """CATEGORIES="work:Work,side:Side,life:Life" → {folder: label}, in display order.
    Folder = tasks/<folder>/ in the vault; label = the chip / filter / [TAG] text."""
    raw = os.environ.get("CATEGORIES") or "work:Work,side:Side,life:Life"
    out = {}
    for part in raw.split(","):
        folder, _, label = part.strip().partition(":")
        folder = re.sub(r"[^a-z0-9_-]", "", folder.strip().lower())
        if folder:
            out[folder] = (label or folder).strip()
    return out or {"work": "Work"}


# Top-level categories: folder names under tasks/ → chip / filter label (from .env).
CATEGORY_DIRS = _categories()
LEGACY_CATEGORY_DIRS: dict = {}
DOMAIN_ALIASES = {**{v: v for v in CATEGORY_DIRS.values()}, **{v.upper(): v for v in CATEGORY_DIRS.values()}}
DOMAIN_TAG = "(" + "|".join(re.escape(a) for a in sorted(DOMAIN_ALIASES, key=len, reverse=True)) + ")"
# Folder segment in tasks/<cat>/… paths.
CAT_FOLDER_RE = "(?:" + "|".join(re.escape(d) for d in CATEGORY_DIRS) + ")"
# Default visible rows before the list scrolls (Today boxes / By category / nested).
LIST_CAP = 6
DOMAIN_ORDER = tuple(CATEGORY_DIRS.values())
# Category picked by default in "Add a task" rows that let you choose one.
DEFAULT_CATEGORY = os.environ.get("DEFAULT_CATEGORY") or DOMAIN_ORDER[-1]
if DEFAULT_CATEGORY not in DOMAIN_ORDER:
    DEFAULT_CATEGORY = DOMAIN_ORDER[-1]

# Category colours: one palette slot per category, in order (light, light-bg, dark, dark-bg).
_PALETTE = [("#3f6f99", "#e8f0f7", "#8db6dc", "#23303b"), ("#5d7f43", "#edf3e7", "#a5c98a", "#28331f"),
            ("#9b7432", "#f6efe2", "#d9b06f", "#3a3122"), ("#4a6b6e", "#e6efef", "#8fb4b7", "#243334"),
            ("#7a4f9a", "#f1eaf6", "#c3a1e0", "#30263a"), ("#a04a5a", "#f8e9ec", "#e597a6", "#3a2228")]
PALETTE_LIGHT = " ".join(f"--c{i}:{c[0]}; --c{i}-bg:{c[1]};" for i, c in enumerate(_PALETTE))
PALETTE_DARK = " ".join(f"--c{i}:{c[2]}; --c{i}-bg:{c[3]};" for i, c in enumerate(_PALETTE))
CATEGORY_CSS = "\\n".join(
    f".chip.{d}, .{d} {{ color:var(--c{i % len(_PALETTE)}); background:var(--c{i % len(_PALETTE)}-bg) }}\\n"
    f".box.list-{d} {{ border-top-color:var(--c{i % len(_PALETTE)}) }}\\n"
    f'.filt-cat[data-cat="{lbl}"] {{ --c:var(--c{i % len(_PALETTE)}); --cb:var(--c{i % len(_PALETTE)}-bg) }}'
    for i, (d, lbl) in enumerate(CATEGORY_DIRS.items()))
'''

ENV_SOURCE = ('INFRA="${ASSISTANT_INFRA:-$(cd "$SCRIPT_DIR/.." && pwd)}"\n'
              '# Settings from the kit\'s .env (see .env.example); the real environment wins.\n'
              'if [[ -f "$INFRA/.env" ]]; then\n'
              '  while IFS= read -r _l; do\n'
              '    [[ "$_l" =~ ^[[:space:]]*(export[[:space:]]+)?([A-Za-z_][A-Za-z0-9_]*)=(.*)$ ]] || continue\n'
              '    _k="${BASH_REMATCH[2]}"; _v="${BASH_REMATCH[3]}"; _v="${_v%\\"}"; _v="${_v#\\"}"\n'
              '    _v="${_v/#\\~/$HOME}"; _v="${_v//\\$HOME/$HOME}"\n'
              '    [[ -n "${!_k+x}" ]] || export "$_k=$_v"\n'
              '  done < "$INFRA/.env"\n'
              'fi\n')

# The rules (MODES: mode -> [(old, new, expected count)], DENY: regex of strings that must
# not survive) quote the personal text they remove, so they live OUTSIDE the kit:
# $KIT_RULES, default ~/.config/task-dashboard-kit/rules.py. See scripts/rules.example.py.
RULES_FILE = Path(os.environ.get("KIT_RULES") or Path.home() / ".config/task-dashboard-kit/rules.py")


def load_rules() -> tuple[dict, "re.Pattern"]:
    if not RULES_FILE.is_file():
        raise SystemExit(f"generalize: no rules file at {RULES_FILE} (copy scripts/rules.example.py there)")
    ns = {"re": re, "CONFIG_BLOCK": CONFIG_BLOCK, "ENV_SOURCE": ENV_SOURCE}
    exec(compile(RULES_FILE.read_text(), str(RULES_FILE), "exec"), ns)
    modes = {"plain": [], **ns["MODES"]}
    return modes, ns.get("DENY") or re.compile(r"(?!x)x")


def generalize(src: str, mode: str = "dashboard") -> str:
    modes, DENY = load_rules()
    out = src
    for old, new, n in modes[mode]:
        if isinstance(old, re.Pattern):
            out, k = old.subn(lambda m: new, out)
        else:
            k = out.count(old)
            out = out.replace(old, new)
        if (n is None and k == 0) or (n is not None and k != n):
            raise SystemExit(f"generalize: rule matched {k}x (expected {n if n is not None else '≥1'}):\n"
                             f"  {str(getattr(old, 'pattern', old))[:120]!r}\n"
                             "The source changed here: update RULES in scripts/generalize.py.")
    left = sorted({m.group(0) for m in DENY.finditer(out)})
    if left:
        lines = [f"  {i + 1}: {l.strip()[:110]}" for i, l in enumerate(out.splitlines()) if DENY.search(l)]
        raise SystemExit("generalize: personal strings left: " + ", ".join(left) + "\n" + "\n".join(lines[:30]))
    return out


if __name__ == "__main__":
    if len(sys.argv) != 4 or sys.argv[1] not in {"plain", *load_rules()[0]}:
        raise SystemExit(__doc__)
    mode, src, dst = sys.argv[1], Path(sys.argv[2]), Path(sys.argv[3])
    try:
        text = generalize(src.read_text(), mode)
    except SystemExit as e:
        raise SystemExit(f"[{src}] {e}")
    dst.parent.mkdir(parents=True, exist_ok=True)
    dst.write_text(text)
    if src.stat().st_mode & 0o111:
        dst.chmod(0o755)
    print(f"generalize: {mode:14s} {dst}")
