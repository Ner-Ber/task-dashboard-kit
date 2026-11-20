"""Notes tab: path containment, uploads, Obsidian import, sync conflicts, link-notes."""
import base64
import datetime as dt
import importlib.util
import os
import subprocess
from pathlib import Path

import pytest

spec = importlib.util.spec_from_file_location(
    "dashboard", Path(__file__).parent.parent / "app/dashboard.py")
D = importlib.util.module_from_spec(spec)
spec.loader.exec_module(D)

PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg==")
WEBP = b"RIFF\x1a\x00\x00\x00WEBPVP8L\x0d\x00\x00\x00/\x00\x00\x00\x10\x07\x10\x11\x11\x88\x88\xfe\x07\x00"


def sh(cwd, *a, env=None):
    e = dict(os.environ, **(env or {}))
    return subprocess.run(a, cwd=cwd, check=True, capture_output=True, text=True, env=e).stdout


def mkrepo(tmp_path, name="vault"):
    remote, v = tmp_path / f"{name}-remote.git", tmp_path / name
    sh(tmp_path, "git", "init", "-q", "--bare", str(remote))
    sh(tmp_path, "git", "--git-dir", str(remote), "symbolic-ref", "HEAD", "refs/heads/master")
    sh(tmp_path, "git", "clone", "-q", str(remote), str(v))
    sh(v, "git", "config", "user.email", "t@t"), sh(v, "git", "config", "user.name", "t")
    return remote, v


@pytest.fixture
def vault(tmp_path, monkeypatch):
    """Vault (task lists) + sibling notes repo (tmp/notes), each with a bare origin."""
    monkeypatch.delenv("ASSISTANT_NOTES", raising=False)
    monkeypatch.setattr(D, "_notes_last_pull", 0.0)
    remote, v = mkrepo(tmp_path)
    for d in ("tasks/work", "tasks/work/website", "tasks/life"):
        (v / d).mkdir(parents=True, exist_ok=True)
        (v / d / "TASKS.md").write_text(f"# {d.split('/')[-1].upper()}\n\n## Active\n- [ ] Do it ➕ 2026-01-01 🆔 abc{len(d):03x}\n\n## Done\n")
    (v / "memory/projects").mkdir(parents=True)
    (v / "memory/projects/website.md").write_text("# Website\n\n**Repo:** `example/website`\n")
    sh(v, "git", "add", "-A"), sh(v, "git", "commit", "-qm", "init"), sh(v, "git", "push", "-q", "-u", "origin", "master")
    _, n = mkrepo(tmp_path, "notes")
    (n / "work/website").mkdir(parents=True)
    (n / "work/website/Progress.md").write_text("# Progress\n\nfirst line\n")
    sh(n, "git", "add", "-A"), sh(n, "git", "commit", "-qm", "init"), sh(n, "git", "push", "-q", "-u", "origin", "master")
    D._notes_conflicts.clear()
    return v


def NOTES(v):
    return v.parent / "notes"


# ---------------------------------------------------------------- containment
def test_notes_root_is_sibling_or_env(vault, monkeypatch, tmp_path):
    assert D.notes_root(vault) == NOTES(vault)
    monkeypatch.setenv("ASSISTANT_NOTES", str(tmp_path / "elsewhere"))
    assert D.notes_root(vault) == tmp_path / "elsewhere"
    with pytest.raises(LookupError):
        D.notes_repo(vault)


def test_notes_projects_lists_every_task_folder(vault):
    ps = {p["key"]: p for p in D.notes_projects(vault)}
    assert set(ps) == {"work", "work/website", "life"}
    assert ps["work/website"]["notes"][0]["name"] == "Progress" and ps["work"]["kind"] == "category"
    assert ps["life"]["domain"] == "Life"


@pytest.mark.parametrize("key", ["../work", "work/../life", "/etc", "work/none", "", "work//website", "work\\website"])
def test_bad_project_keys_are_refused(vault, key):
    with pytest.raises(D.NotesPathError):
        D.notes_dir(vault, key)


@pytest.mark.parametrize("rel", ["../TASKS.md", "/etc/passwd", "attachments/../../TASKS.md", "a//b.md",
                                 "C:/x.md", "a\\b.md", "./x.md", "", "other/x.md", "attachments/a/b.png",
                                 "../../life/x.md"])
def test_paths_must_stay_inside_notes(vault, rel):
    with pytest.raises(D.NotesPathError):
        D.notes_path(vault, "work/website", rel)


def test_symlinks_out_of_notes_are_refused(vault, tmp_path):
    secret = tmp_path / "secret"
    secret.mkdir()
    (secret / "x.png").write_bytes(PNG)
    (NOTES(vault) / "work/website/attachments").symlink_to(secret)
    with pytest.raises(D.NotesPathError):
        D.notes_path(vault, "work/website", "attachments/x.png")
    (NOTES(vault) / "work/website/evil.md").symlink_to(vault / "tasks/work/TASKS.md")
    with pytest.raises(D.NotesPathError):
        D.note_read(vault, "work/website", "evil")


def test_symlinked_notes_folder_is_refused(vault, tmp_path):
    other = tmp_path / "outside"
    other.mkdir()
    (NOTES(vault) / "life").symlink_to(other)
    with pytest.raises(D.NotesPathError):
        D.notes_dir(vault, "life")


def test_symlinked_project_folder_is_refused(vault, tmp_path):
    other = tmp_path / "elsewhere"
    other.mkdir()
    (other / "TASKS.md").write_text("# X\n")
    (vault / "tasks/work/linked").symlink_to(other)
    with pytest.raises(D.NotesPathError):
        D.notes_dir(vault, "work/linked")


@pytest.mark.parametrize("name", ["a/b", "..", ".hidden", "x?", "trailing.", " pad", "a\\b"])
def test_bad_note_names(vault, name):
    with pytest.raises(D.NotesPathError):
        D.note_file(vault, "work/website", name)


def test_hebrew_note_names_work(vault):
    D.note_create(vault, "work/website", "סיכום פגישה")
    assert D.note_read(vault, "work/website", "סיכום פגישה")["text"].startswith("# סיכום פגישה")


# ---------------------------------------------------------------- uploads
def test_upload_saves_dated_attachment(vault):
    now = dt.datetime(2026, 11, 20, 9, 30, 5)
    r = D.note_upload(vault, "work/website", "image/webp", WEBP, now=now)
    assert r["path"] == "attachments/2026-11-20-093005.webp"
    r2 = D.note_upload(vault, "work/website", "image/png", PNG, now=now)
    assert r2["path"] == "attachments/2026-11-20-093005.png"
    r3 = D.note_upload(vault, "work/website", "image/webp", WEBP, now=now)
    assert r3["path"] == "attachments/2026-11-20-093005-1.webp"
    body, ctype = D.note_asset(vault, "work/website", r["path"])
    assert body == WEBP and ctype == "image/webp"


@pytest.mark.parametrize("ctype,data", [("text/html", b"<script>"), ("image/svg+xml", b"<svg/>"),
                                        ("image/png", WEBP), ("image/webp", b"RIFF0000JUNK"), ("image/png", b"")])
def test_upload_rejects_non_images(vault, ctype, data):
    with pytest.raises(ValueError):
        D.note_upload(vault, "work/website", ctype, data)


def test_upload_size_cap(vault, monkeypatch):
    monkeypatch.setattr(D, "MAX_UPLOAD", 10)
    with pytest.raises(ValueError):
        D.note_upload(vault, "work/website", "image/png", PNG)


def test_asset_endpoint_serves_images_only(vault):
    with pytest.raises(LookupError):
        D.note_asset(vault, "work/website", "Progress.md")


# ---------------------------------------------------------------- save + conflicts
def test_save_with_stale_base_keeps_both_versions(vault):
    n = D.note_read(vault, "work/website", "Progress")
    (NOTES(vault) / "work/website/Progress.md").write_text("# Progress\n\nedited elsewhere\n")
    r = D.note_save(vault, "work/website", "Progress", "# Progress\n\nmine\n", n["hash"])
    assert r["conflict"] and (NOTES(vault) / "work/website/Progress.md").read_text().endswith("mine\n")
    copy = NOTES(vault) / "work/website" / (r["conflict"]["copy"] + ".md")
    assert copy.read_text().endswith("edited elsewhere\n")
    # Same base, unchanged disk: no conflict.
    r2 = D.note_save(vault, "work/website", "Progress", "# Progress\n\nmine 2\n", r["hash"])
    assert r2["conflict"] is None


def test_sync_commits_one_batch_and_pushes(vault):
    D.note_save(vault, "work/website", "Progress", "# Progress\n\nmore\n", None)
    D.note_create(vault, "work/website", "Ideas")
    D.note_upload(vault, "work/website", "image/png", PNG)
    n = NOTES(vault)
    before = int(sh(n, "git", "rev-list", "--count", "HEAD"))
    r = D.notes_sync(vault)
    assert r["committed"] and not r["dirty"] and not r["conflicts"]
    assert int(sh(n, "git", "rev-list", "--count", "HEAD")) == before + 1
    msg = sh(n, "git", "log", "-1", "--format=%s").strip()
    assert msg.startswith("notes: work/website/") and "(+1 more)" in msg
    assert sh(n, "git", "status", "--porcelain").strip() == ""
    assert sh(n, "git", "rev-parse", "HEAD") == sh(n, "git", "rev-parse", "@{u}")
    idx = (n / "PROJECTS.md").read_text()
    assert "| `work/website/` | WEBSITE | `example/website` |" in idx
    assert sh(vault, "git", "status", "--porcelain").strip() == ""   # vault untouched


def other_clone(tmp_path):
    other = tmp_path / "other"
    sh(tmp_path, "git", "clone", "-q", str(tmp_path / "notes-remote.git"), str(other))
    sh(other, "git", "config", "user.email", "o@o"), sh(other, "git", "config", "user.name", "o")
    return other


def test_sync_conflict_keeps_mine_and_saves_remote_copy(vault, tmp_path):
    other = other_clone(tmp_path)
    (other / "work/website/Progress.md").write_text("# Progress\n\nfrom the server\n")
    (other / "work/website/Other.md").write_text("new elsewhere\n")
    sh(other, "git", "add", "-A"), sh(other, "git", "commit", "-qm", "remote edit"), sh(other, "git", "push", "-q")
    D.note_save(vault, "work/website", "Progress", "# Progress\n\nfrom the laptop\n", None)
    r = D.notes_sync(vault)
    assert len(r["conflicts"]) == 1
    n = NOTES(vault) / "work/website"
    assert (n / "Progress.md").read_text().endswith("from the laptop\n")
    assert (n / (r["conflicts"][0]["copy"] + ".md")).read_text().endswith("from the server\n")
    assert (n / "Other.md").is_file()                                   # remote's other change merged
    assert sh(NOTES(vault), "git", "rev-parse", "HEAD") == sh(NOTES(vault), "git", "rev-parse", "@{u}")
    assert not D._rebase_in_progress(NOTES(vault))


def test_refresh_pulls_only_when_clean(vault, tmp_path):
    other = other_clone(tmp_path)
    (other / "work/website/Remote.md").write_text("x\n")
    sh(other, "git", "add", "-A"), sh(other, "git", "commit", "-qm", "r"), sh(other, "git", "push", "-q")
    D.note_save(vault, "work/website", "Progress", "local\n", None)
    D.notes_refresh(vault)
    assert not (NOTES(vault) / "work/website/Remote.md").exists()           # unsynced edits: no pull
    D.notes_sync(vault)
    assert (NOTES(vault) / "work/website/Remote.md").exists()


def test_sync_script_in_notes_repo_keeps_both_versions(vault, tmp_path):
    script = Path(__file__).parent.parent / "templates/notes/.notes/sync.sh"
    if not script.is_file():
        pytest.skip("notes repo sync script not on this machine")
    n = NOTES(vault)
    (n / ".notes").mkdir()
    (n / ".notes/sync.sh").write_bytes(script.read_bytes())
    (n / ".notes/sync.sh").chmod(0o755)
    sh(n, "git", "add", "-A"), sh(n, "git", "commit", "-qm", "script"), sh(n, "git", "push", "-q")
    other = other_clone(tmp_path)
    (other / "work/website/Progress.md").write_text("from the server\n")
    sh(other, "git", "commit", "-qam", "r"), sh(other, "git", "push", "-q")
    (n / "work/website/Progress.md").write_text("from the laptop\n")
    subprocess.run([str(n / ".notes/sync.sh")], cwd=n, capture_output=True, text=True)
    assert (n / "work/website/Progress.md").read_text() == "from the laptop\n"
    copies = list((n / "work/website").glob("Progress.conflict-*.md"))
    assert len(copies) == 1 and copies[0].read_text() == "from the server\n"
    assert sh(n, "git", "status", "--porcelain").strip() == ""
    assert sh(n, "git", "rev-parse", "HEAD") == sh(n, "git", "rev-parse", "@{u}")


def test_commit_message_names_note():
    assert D.notes_commit_msg(["work/website/Progress.md"]) == "notes: work/website/Progress"
    assert D.notes_commit_msg(["life/attachments/x.webp"]) == "notes: life attachments"


# ---------------------------------------------------------------- Obsidian conversion / import
def test_convert_obsidian_embeds_and_links():
    attach = lambda n: {"img.png": "attachments/img.png", "Pasted image 1.png": "attachments/Pasted-image-1.png"}.get(n)
    link = lambda n: {"Other note": "Other note.md", "Plan": "../../research/notes/Plan.md"}.get(n)
    src = ("a ![[img.png]] b ![[img.png|300]] ![[Pasted image 1.png]]\n"
           "see [[Other note]] and [[Plan|the log]] and [[Plan#Sep 7]] and [[Missing]]\n"
           "![[missing.png]] ![x](img.png)\n```\n![[img.png]]\n```\n")
    out = convert_out = D.convert_obsidian(src, attach, link)
    assert "a ![](attachments/img.png) b ![](attachments/img.png) ![](attachments/Pasted-image-1.png)" in out
    assert "[Other note](Other%20note.md)" in out
    assert "[the log](../../research/notes/Plan.md)" in out
    assert "[Plan › Sep 7](../../research/notes/Plan.md#sep-7)" in out
    assert "[[Missing]]" in out and "![[missing.png]]" in out
    assert "![x](attachments/img.png)" in out
    assert "```\n![[img.png]]\n```" in convert_out


def test_import_obsidian_copies_and_commits_per_project(vault, tmp_path):
    obs = tmp_path / "obs"
    (obs / "pics").mkdir(parents=True)
    (obs / ".obsidian").mkdir()
    (obs / "pics/Pasted image 1.png").write_bytes(PNG)
    (obs / "Log.md").write_text("# Log\n![[Pasted image 1.png|200]]\nsee [[Ideas]]\n")
    (obs / "Ideas.md").write_text("# Ideas\nback to [[Log]]\n")
    (obs / "Skip.md").write_text("x")
    n = NOTES(vault)
    before = int(sh(n, "git", "rev-list", "--count", "HEAD"))
    dry = D.import_obsidian(obs, vault, {"Log.md": "work/website", "Ideas.md": "life", "Skip.md": None}, dry_run=True)
    assert len(dry["notes"]) == 2 and not (n / "work/website/Log.md").exists()
    r = D.import_obsidian(obs, vault, {"Log.md": "work/website", "Ideas.md": "life", "Skip.md": None})
    log = (n / "work/website/Log.md").read_text()
    assert "![](attachments/Pasted-image-1.png)" in log and "[Ideas](../../life/Ideas.md)" in log
    assert (n / "work/website/attachments/Pasted-image-1.png").read_bytes() == PNG
    assert "[Log](../work/website/Log.md)" in (n / "life/Ideas.md").read_text()
    assert len(r["commits"]) == 2 and int(sh(n, "git", "rev-list", "--count", "HEAD")) == before + 2
    assert (obs / "Log.md").read_text().startswith("# Log\n![[")      # source untouched
    with pytest.raises(LookupError):   # never overwrites a different existing note
        (obs / "Log.md").write_text("changed")
        D.import_obsidian(obs, vault, {"Log.md": "work/website"})


# ---------------------------------------------------------------- link-notes
def test_link_notes_symlink_hidden_from_git(vault, tmp_path):
    _, code = mkrepo(tmp_path, "website-code")
    (code / "README.md").write_text("x")
    sh(code, "git", "add", "-A"), sh(code, "git", "commit", "-qm", "init")
    cfg = tmp_path / "cfg"
    r = D.link_notes(vault, "website", code, cfg)
    assert r["method"] == "symlink" and r["key"] == "work/website"
    link = code / "progress"
    assert link.is_symlink() and (link / "Progress.md").is_file()
    assert link.resolve() == (NOTES(vault) / "work/website").resolve()
    assert sh(code, "git", "status", "--porcelain", "--untracked-files=all") == ""
    assert "/progress" in (code / ".git/info/exclude").read_text()
    assert not (code / ".gitignore").exists()
    assert D.read_links(cfg) == {"work/website": str(code.resolve())}
    # Idempotent: no duplicate exclude line, same link accepted.
    r2 = D.link_notes(vault, "work/website", code, cfg)
    assert r2["excluded"] is False
    assert (code / ".git/info/exclude").read_text().count("/progress") == 1


def test_link_notes_refuses_foreign_progress_folder(vault, tmp_path):
    _, code = mkrepo(tmp_path, "website-code")
    (code / "progress").mkdir()
    with pytest.raises(LookupError):
        D.link_notes(vault, "website", code, tmp_path / "cfg")


def test_add_exclude_works_in_a_linked_worktree(tmp_path):
    _, code = mkrepo(tmp_path, "repo")
    (code / "f").write_text("x")
    sh(code, "git", "add", "-A"), sh(code, "git", "commit", "-qm", "init")
    wt = tmp_path / "wt"
    sh(code, "git", "worktree", "add", "-q", "-b", "side", str(wt))
    D.add_exclude(wt)
    (wt / "progress").mkdir()
    (wt / "progress/x").write_text("y")
    assert D.git_sees(wt) == ""


def test_static_files_stay_inside_static():
    assert D.static_file("vendor/marked/marked.min.js")
    assert D.static_file("notes/notes.js")
    for bad in ("../dashboard.py", "vendor/../../dashboard.py", "/etc/passwd", "notes/../../SKILL.md", ""):
        assert D.static_file(bad) is None
