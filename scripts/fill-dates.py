#!/usr/bin/env python3
"""Turn the date placeholders in the sample data into real dates, relative to today.

    python3 scripts/fill-dates.py <dir> [<dir> …] [--today YYYY-MM-DD]

Used by scripts/init-data-repos.sh right after copying templates/, so the sample tasks
and notes always look fresh (due today, overdue by a day, …) instead of stale.

Placeholders, in every *.md file under the given directories:
  {{d+N}} / {{d-N}} / {{d+0}}   ISO date N days from today      → 2026-11-27
  {{h+N}} / {{h-N}} / {{h+0}}   progress-note heading date      → Nov 27, 2026
"""
import datetime as dt
import re
import sys
from pathlib import Path

TOKEN = re.compile(r"\{\{([dh])([+-]\d+)\}\}")


def fill(text: str, today: dt.date) -> str:
    def sub(m: re.Match) -> str:
        day = today + dt.timedelta(days=int(m.group(2)))
        return day.isoformat() if m.group(1) == "d" else f"{day:%b} {day.day}, {day.year}"
    return TOKEN.sub(sub, text)


def main(argv: list[str]) -> int:
    today = dt.date.today()
    if "--today" in argv:
        i = argv.index("--today")
        today = dt.date.fromisoformat(argv[i + 1])
        argv = argv[:i] + argv[i + 2:]
    if not argv:
        print(__doc__, file=sys.stderr)
        return 2
    for root in map(Path, argv):
        for f in root.rglob("*.md"):
            if ".git" in f.parts:
                continue
            text = f.read_text()
            new = fill(text, today)
            if new != text:
                f.write_text(new)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
