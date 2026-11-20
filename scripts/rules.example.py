"""Example PRIVATE rules for scripts/generalize.py (used by scripts/export-from-source.sh).

Copy to ~/.config/task-dashboard-kit/rules.py (or point $KIT_RULES at it) and fill in the
personal strings of YOUR source setup. Keep the real file out of every repository: it
quotes exactly the personal text it removes.

generalize.py provides these names here: re, CONFIG_BLOCK (the .env-driven settings block
for dashboard.py) and ENV_SOURCE (a bash snippet that loads the kit's .env).

Each rule is (old, new, expected_count):
  old             a string, or a compiled regex
  new             the replacement
  expected_count  exact number of matches required (None = at least one). A mismatch stops
                  the export and names the rule, so a changed source never slips through.
"""

MODES = {
    # app/dashboard.py
    "dashboard": [
        # Replace your hard-coded categories with the .env-driven block:
        (re.compile(r"# Prefer ASSISTANT_VAULT.*?\nDOMAIN_ORDER = \([^\n]*\)\n", re.S), CONFIG_BLOCK.lstrip("\n"), 1),
        ('DASHBOARD_BRAND = "Alex Dashboard"', 'DASHBOARD_BRAND = os.environ.get("DASHBOARD_BRAND", "Tasks")', 0),
        ("Alex's instruction", "the user's instruction", None),
    ],
    "open-dashboard": [
        ("# Open Alex's live dashboard", "# Open the live dashboard", 1),
        ("/skills/dashboard/dashboard.py", "/app/dashboard.py", None),
    ],
    "shortcuts": [],
    "test-notes": [("acme-lab/secret-project", "example/website", None)],
    "agent-prompt": [("`alex/lab-notes`", "`<OWNER>/<NOTES_REPO>`", None)],
    "notes-css": [],
    "notes-js": [],
    "notes-html": [],
}

# Anything matching this must not survive any mode (the export fails if it does).
DENY = re.compile(r"Alex|alex-|acme-lab|secret-project")
