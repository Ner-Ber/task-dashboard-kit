"""Kit-level checks: link-notes, category config, the templates render, privacy."""
import importlib.util
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

KIT = Path(__file__).resolve().parent.parent
LINK = KIT / "scripts/link-notes.sh"


def sh(cwd, *a, check=True, env=None):
    return subprocess.run(a, cwd=cwd, capture_output=True, text=True, check=check,
                          env={**os.environ, **(env or {})})


def load_dashboard(monkeypatch, **env):
    for k in ("CATEGORIES", "DEFAULT_CATEGORY", "DASHBOARD_BRAND", "ASSISTANT_NOTES", "ASSISTANT_VAULT"):
        monkeypatch.delenv(k, raising=False)
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    monkeypatch.chdir(KIT / "tests")            # no stray .env in cwd
    spec = importlib.util.spec_from_file_location(f"dash_{len(env)}", KIT / "app/dashboard.py")
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


@pytest.fixture
def code_repo(tmp_path):
    r = tmp_path / "code"
    r.mkdir()
    sh(r, "git", "init", "-q")
    (r / "README.md").write_text("public code\n")
    sh(r, "git", "add", "-A")
    sh(r, "git", "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "init")
    return r


@pytest.fixture
def notes(tmp_path):
    n = tmp_path / "notes"
    shutil.copytree(KIT / "templates/notes", n)
    return n


def test_link_notes_link_verify_unlink(code_repo, notes):
    r = sh(KIT, str(LINK), "work/website", str(code_repo), env={"ASSISTANT_NOTES": str(notes)})
    assert "verified" in r.stdout
    assert (code_repo / "progress/Progress.md").is_file()
    assert sh(code_repo, "git", "status", "--porcelain", "--untracked-files=all").stdout == ""
    assert "/progress" in (code_repo / ".git/info/exclude").read_text()
    assert not (code_repo / ".gitignore").exists()
    # idempotent
    r2 = sh(KIT, str(LINK), "work/website", str(code_repo), env={"ASSISTANT_NOTES": str(notes)})
    assert "already in place" in r2.stdout
    assert (code_repo / ".git/info/exclude").read_text().count("/progress") == 1
    # unlink
    sh(KIT, str(LINK), "--unlink", str(code_repo))
    assert not (code_repo / "progress").exists()
    assert "/progress" not in (code_repo / ".git/info/exclude").read_text()
    assert sh(code_repo, "git", "status", "--porcelain").stdout == ""
    assert (notes / "work/website/Progress.md").is_file()        # notes untouched


def test_link_notes_refuses_tracked_or_existing(code_repo, notes):
    (code_repo / "progress").mkdir()
    (code_repo / "progress/x.txt").write_text("tracked")
    sh(code_repo, "git", "add", "-A")
    r = sh(KIT, str(LINK), "work/website", str(code_repo), check=False, env={"ASSISTANT_NOTES": str(notes)})
    assert r.returncode != 0 and "tracked by git" in r.stderr
    r = sh(KIT, str(LINK), "work/website", str(code_repo), "--name", "notes-dir", check=False,
           env={"ASSISTANT_NOTES": str(notes)})
    assert r.returncode == 0
    (code_repo / "other").mkdir()
    r = sh(KIT, str(LINK), "work/website", str(code_repo), "--name", "other", check=False,
           env={"ASSISTANT_NOTES": str(notes)})
    assert r.returncode != 0 and "not a link" in r.stderr


def test_link_notes_in_linked_worktree(code_repo, notes, tmp_path):
    wt = tmp_path / "wt"
    sh(code_repo, "git", "worktree", "add", "-q", "-b", "side", str(wt))
    sh(KIT, str(LINK), str(notes / "side/garden-sensor"), str(wt))
    assert sh(wt, "git", "status", "--porcelain", "--untracked-files=all").stdout == ""


def test_categories_come_from_env(monkeypatch):
    m = load_dashboard(monkeypatch, CATEGORIES="lab:Lab,home:Home", DEFAULT_CATEGORY="Lab", DASHBOARD_BRAND="My Tasks")
    assert m.CATEGORY_DIRS == {"lab": "Lab", "home": "Home"}
    assert m.DOMAIN_ORDER == ("Lab", "Home") and m.DEFAULT_CATEGORY == "Lab"
    assert m.normalize_domain("LAB") == "Lab" and m.normalize_domain("Home") == "Home"
    assert ".chip.home" in m.CATEGORY_CSS and 'data-cat="Lab"' in m.CATEGORY_CSS
    d = load_dashboard(monkeypatch)
    assert d.DOMAIN_ORDER == ("Work", "Side", "Life") and d.DASHBOARD_BRAND == "Tasks"


def test_templates_render(monkeypatch, tmp_path):
    vault = tmp_path / "vault"
    shutil.copytree(KIT / "templates/vault", vault)
    shutil.copytree(KIT / "templates/notes", tmp_path / "notes")
    sh(KIT, "python3", str(KIT / "scripts/fill-dates.py"), str(vault), str(tmp_path / "notes"))
    for d in (vault, tmp_path / "notes"):
        assert not [f for f in d.rglob("*.md") if "{{" in f.read_text()], "unfilled date placeholder"
        sh(d, "git", "init", "-q")
    m = load_dashboard(monkeypatch)
    data = m.collect(vault, activity={})
    titles = {p["title"] for p in data["projects"]}
    assert {"Start here", "Website relaunch", "Garden sensor", "Reading group"} <= titles
    keys = {(l["domain"], l.get("project")) for l in data["task_lists"]}
    assert ("Work", "website") in keys and ("Side", "garden-sensor") in keys
    page = m.render(data, {}, live=True)
    assert "Book the quarterly planning room" in page and "<title>Tasks</title>" in page
    notes = {p["key"]: [n["name"] for n in p["notes"]] for p in m.notes_projects(vault)}
    assert notes["work/website"] == ["Progress"] and notes["side/garden-sensor"] == ["Progress"]
    assert "Progress" in notes["work/start-here"]
    assert any(".conflict-" in n for n in notes["work/start-here"])
    body, ctype = m.note_asset(vault, "work/website", "attachments/2026-11-18-101500.png")
    assert ctype == "image/png" and body[:4] == b"\x89PNG"


def test_skills_have_frontmatter():
    for s in (KIT / "skills").glob("*/SKILL.md"):
        head = s.read_text().split("---")[1]
        assert f"name: {s.parent.name}" in head and "description:" in head


def test_privacy_check_passes():
    r = sh(KIT, str(KIT / "scripts/privacy-check.sh"), check=False)
    assert r.returncode == 0, r.stdout + r.stderr


def test_fill_dates_relative_to_today(tmp_path):
    f = tmp_path / "TASKS.md"
    f.write_text("- [ ] x ➕ {{d+0}} 📅 {{d+3}} ✅ {{d-1}}\n## {{h-2}} - y\n")
    sh(KIT, "python3", str(KIT / "scripts/fill-dates.py"), str(tmp_path), "--today", "2026-12-30")
    assert f.read_text() == "- [ ] x ➕ 2026-12-30 📅 2027-01-02 ✅ 2026-12-29\n## Dec 28, 2026 - y\n"


def test_start_here_tutorial_ids_unique():
    import re
    ids = re.findall(r"🆔 ([0-9a-f]{6})", "".join(p.read_text() for p in (KIT / "templates/vault").rglob("*.md")))
    assert len(ids) == len(set(ids)) and all(len(i) == 6 for i in ids)
