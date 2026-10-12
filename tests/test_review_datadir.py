"""User data is local, never in the repo, and portable via backup/restore."""
import json
import os
import tarfile

import pytest

from dandelion import cli, datadir
from dandelion import paper as paperlib


def _seed(root):
    (root / "papers" / "uploads").mkdir(parents=True)
    (root / "dialogs-kernel.json").write_text('{"d": []}')
    (root / "dialogs-kernel.json.bak").write_text("old")
    (root / "recall.json").write_text("{}")
    (root / "blog" / "posts").mkdir(parents=True)
    (root / "blog" / "posts" / "p.qmd").write_text("post")
    pdf = root / "papers" / "uploads" / "abc.pdf"
    pdf.write_bytes(b"%PDF")
    (root / "papers" / f"{paperlib._md_key(pdf)}.md").write_text("# conv")
    (root / "papers" / "assets" / "k1").mkdir(parents=True)
    (root / "papers" / "assets" / "k1" / "f.jpeg").write_bytes(b"img")


def _env(monkeypatch, root):
    monkeypatch.setenv("DANDELION_DATA", str(root))
    monkeypatch.setenv("DANDELION_PAPERS", str(root / "papers"))
    monkeypatch.setenv("DANDELION_SECRETS", str(root / "secrets.json"))
    monkeypatch.setenv("DANDELION_TOOLS", str(root / "tools.json"))


def test_default_root_is_home_config(tmp_path, monkeypatch):
    monkeypatch.delenv("DANDELION_DATA", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))
    assert datadir.data_root() == tmp_path / ".config" / "dandelion"


def test_every_store_uses_the_data_root(tmp_path, monkeypatch):
    from dandelion import blog, client, libraries, recall
    monkeypatch.setenv("DANDELION_DATA", str(tmp_path / "r"))
    r = tmp_path / "r"
    assert client._store_path("kernel").parent == r
    assert recall.default_schedule_path() == r / "recall.json"
    assert libraries._path() == r / "libraries.json"
    assert blog.default_blog_dir() == str(r / "blog")


def test_repo_data_is_gitignored():
    gi = (datadir.REPO_DATA.parent / ".gitignore").read_text().splitlines()
    assert "data/" in gi


def test_migrate_repo_data_moves_without_overwriting(tmp_path, monkeypatch):
    old = tmp_path / "repo-data"
    _seed(old)
    new = tmp_path / "home"
    new.mkdir()
    (new / "recall.json").write_text('{"mine": 1}')             # already here
    _env(monkeypatch, new)
    moved = datadir.migrate_repo_data(old)
    assert (new / "dialogs-kernel.json").read_text() == '{"d": []}'
    assert (new / "blog" / "posts" / "p.qmd").read_text() == "post"
    assert (new / "papers" / "assets" / "k1" / "f.jpeg").is_file()
    assert paperlib.cache_path(new / "papers" / "uploads" / "abc.pdf").read_text() == "# conv"
    assert json.loads((new / "recall.json").read_text()) == {"mine": 1}
    assert "recall.json" in datadir.leftovers(old)               # reported, not lost
    assert "dialogs-kernel.json" in moved
    assert datadir.migrate_repo_data(old) == []                  # idempotent


def test_serve_ignores_stale_repo_data_env(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("DANDELION_DATA", str(datadir.REPO_DATA))
    monkeypatch.setattr(datadir, "migrate_repo_data", lambda old=None: [])
    monkeypatch.setattr(datadir, "leftovers", lambda old=None: [])
    monkeypatch.setattr(datadir, "adopt_legacy_root", lambda home=None: None)
    cli._move_out_of_repo()
    assert "DANDELION_DATA" not in os.environ
    assert "ignoring DANDELION_DATA" in capsys.readouterr().err


def test_stale_legacy_repo_data_env_is_dropped_under_every_name(tmp_path, monkeypatch, capsys):
    # an old launcher exported PAPPUS_DATA=<repo>/data; the import-time alias
    # copied it to DANDELION_DATA. Both must go, or uvicorn's reload worker
    # (a fresh interpreter importing dandelion) re-creates DANDELION_DATA from it.
    import subprocess
    import sys
    for k in ("DANDELION_DATA", "PAPPUS_DATA", "SIDEKICK_DATA"):
        monkeypatch.setenv(k, str(datadir.REPO_DATA))
    monkeypatch.setattr(datadir, "migrate_repo_data", lambda old=None: [])
    monkeypatch.setattr(datadir, "leftovers", lambda old=None: [])
    monkeypatch.setattr(datadir, "adopt_legacy_root", lambda home=None: None)
    cli._move_out_of_repo()
    for k in ("DANDELION_DATA", "PAPPUS_DATA", "SIDEKICK_DATA"):
        assert k not in os.environ
    err = capsys.readouterr().err
    assert "ignoring PAPPUS_DATA=" in err and "SIDEKICK_DATA=" in err
    assert "DANDELION_DATA=" not in err                         # only the names the user set
    repo = datadir.REPO_DATA.parent
    code = "import os, dandelion; print(os.environ.get('DANDELION_DATA'))"
    child = subprocess.run([sys.executable, "-c", code],
                           capture_output=True, text=True, env={**os.environ, "PYTHONPATH": str(repo)},
                           cwd=repo, check=True).stdout.strip()
    assert child == "None"


def test_stale_repo_data_env_keeps_a_good_legacy_one(tmp_path, monkeypatch):
    monkeypatch.setenv("DANDELION_DATA", str(datadir.REPO_DATA))
    monkeypatch.setenv("PAPPUS_DATA", str(datadir.REPO_DATA))
    monkeypatch.setenv("SIDEKICK_DATA", str(tmp_path / "mine"))
    cli._ignore_repo_data_env()
    assert os.environ["DANDELION_DATA"] == str(tmp_path / "mine")  # what a child would pick


def test_backup_restore_roundtrip_to_new_install(tmp_path, monkeypatch):
    a = tmp_path / "machineA"
    _seed(a)
    (a / "secrets.json").write_text('{"ANTHROPIC_API_KEY": "sk"}')
    (a / "tools.json").write_text('{"allow": []}')
    _env(monkeypatch, a)
    arc, n = datadir.backup(tmp_path / "b.tar.gz")
    assert oct(arc.stat().st_mode & 0o777) == "0o600"
    names = tarfile.open(arc).getnames()
    assert "data/dialogs-kernel.json" in names
    assert "config/tools.json" in names
    assert not any("secrets" in x for x in names)                # keys opt-in only
    assert not any(x.endswith(".bak") for x in names)
    assert not any(x == "data/tools.json" for x in names)        # not archived twice

    b = tmp_path / "machineB"                                    # a different path:
    _env(monkeypatch, b)                                         # md keys must re-key
    restored, skipped = datadir.restore(arc)
    assert not skipped
    assert (b / "dialogs-kernel.json").read_text() == '{"d": []}'
    assert (b / "tools.json").read_text() == '{"allow": []}'
    assert (b / "papers" / "assets" / "k1" / "f.jpeg").is_file()
    assert paperlib.cache_path(b / "papers" / "uploads" / "abc.pdf").read_text() == "# conv"
    assert not (b / "secrets.json").exists()


def test_backup_with_secrets_and_restore_keeps_them_private(tmp_path, monkeypatch):
    a = tmp_path / "a"
    a.mkdir()
    (a / "secrets.json").write_text('{"K": "v"}')
    _env(monkeypatch, a)
    arc, _ = datadir.backup(tmp_path / "s.tar.gz", with_secrets=True)
    assert "config/secrets.json" in tarfile.open(arc).getnames()
    b = tmp_path / "b"
    _env(monkeypatch, b)
    datadir.restore(arc)
    assert oct((b / "secrets.json").stat().st_mode & 0o777) == "0o600"


def test_restore_keeps_existing_unless_forced(tmp_path, monkeypatch):
    a = tmp_path / "a"
    _seed(a)
    _env(monkeypatch, a)
    arc, _ = datadir.backup(tmp_path / "x.tar.gz")
    (a / "dialogs-kernel.json").write_text("newer")
    _, skipped = datadir.restore(arc)
    assert (a / "dialogs-kernel.json").read_text() == "newer"
    assert "data/dialogs-kernel.json (exists)" in skipped
    datadir.restore(arc, force=True)
    assert (a / "dialogs-kernel.json").read_text() == '{"d": []}'


@pytest.mark.parametrize("evil", ["data/../../escape.txt", "/etc/evil", "other/x", "data"])
def test_restore_refuses_unsafe_members(tmp_path, monkeypatch, evil):
    import io
    arc = tmp_path / "evil.tar.gz"
    with tarfile.open(arc, "w:gz") as t:
        info = tarfile.TarInfo(evil)
        info.size = 3
        t.addfile(info, io.BytesIO(b"bad"))
        link = tarfile.TarInfo("data/link")
        link.type, link.linkname = tarfile.SYMTYPE, "/etc/passwd"
        t.addfile(link)
    root = tmp_path / "root"
    _env(monkeypatch, root)
    restored, skipped = datadir.restore(arc)
    assert restored == []
    assert not (tmp_path / "escape.txt").exists()
    assert not (root / "link").exists()


def test_cli_backup_and_restore(tmp_path, monkeypatch, capsys):
    a = tmp_path / "a"
    _seed(a)
    _env(monkeypatch, a)
    assert cli.main(["backup", str(tmp_path)]) == 0
    arc = next(tmp_path.glob("dandelion-backup-*.tar.gz"))
    b = tmp_path / "b"
    _env(monkeypatch, b)
    assert cli.main(["restore", str(arc)]) == 0
    assert (b / "dialogs-kernel.json").is_file()
    assert cli.main(["restore"]) == 2


def test_restore_refuses_bare_papers_member(tmp_path, monkeypatch):
    import io
    arc = tmp_path / "p.tar.gz"
    with tarfile.open(arc, "w:gz") as t:
        info = tarfile.TarInfo("data/papers")
        info.size = 1
        t.addfile(info, io.BytesIO(b"x"))
    root = tmp_path / "r"
    _env(monkeypatch, root)
    (root / "papers").mkdir(parents=True)
    for force in (False, True):
        restored, skipped = datadir.restore(arc, force=force)
        assert restored == [] and (root / "papers").is_dir()
    assert not list(root.glob("*.restore-tmp"))


def test_restored_secrets_stay_private_under_any_name(tmp_path, monkeypatch):
    a = tmp_path / "a"
    a.mkdir()
    (a / "secrets.json").write_text('{"K": "v"}')
    _env(monkeypatch, a)
    arc, _ = datadir.backup(tmp_path / "s.tar.gz", with_secrets=True)
    b = tmp_path / "b"
    _env(monkeypatch, b)
    monkeypatch.setenv("DANDELION_SECRETS", str(b / "keys.cfg"))
    datadir.restore(arc)
    assert oct((b / "keys.cfg").stat().st_mode & 0o777) == "0o600"


def test_stray_secrets_and_old_archives_not_backed_up(tmp_path, monkeypatch):
    a = tmp_path / "a"
    _seed(a)
    _env(monkeypatch, a)
    monkeypatch.setenv("DANDELION_SECRETS", str(tmp_path / "elsewhere.json"))
    (a / "secrets.json").write_text("stray key")                 # e.g. from old data/
    (a / "old.tar.gz").write_bytes(b"prior backup")
    (a / "blog" / ".git").mkdir()
    (a / "blog" / ".git" / "HEAD").write_text("ref")
    arc, _ = datadir.backup(tmp_path / "n.tar.gz")
    names = tarfile.open(arc).getnames()
    assert not any("secrets" in x for x in names)
    assert not any(x.endswith("old.tar.gz") or "/.git/" in x for x in names)
    assert "data/blog/posts/p.qmd" in names


# ---- renames: solveit-sidekick → pappus → dandelion ----------------------------
@pytest.mark.parametrize("legacy", datadir.LEGACY_ROOT_NAMES)
def test_adopt_legacy_root_moves_and_links(tmp_path, monkeypatch, legacy):
    monkeypatch.delenv("DANDELION_DATA", raising=False)
    old = tmp_path / ".config" / legacy
    old.mkdir(parents=True)
    (old / "dialogs-kernel.json").write_text('{"a": []}')
    new = datadir.adopt_legacy_root(home=tmp_path)
    assert new == tmp_path / ".config" / "dandelion"
    assert (new / "dialogs-kernel.json").read_text() == '{"a": []}'
    assert old.is_symlink() and old.resolve() == new.resolve()
    assert datadir.adopt_legacy_root(home=tmp_path) is None      # idempotent


def test_adopt_legacy_root_follows_the_rename_chain(tmp_path, monkeypatch):
    # The Pappus rename left ~/.config/solveit-sidekick as a link to ~/.config/pappus.
    monkeypatch.delenv("DANDELION_DATA", raising=False)
    cfg = tmp_path / ".config"
    (cfg / "pappus").mkdir(parents=True)
    (cfg / "pappus" / "recall.json").write_text("{}")
    (cfg / "solveit-sidekick").symlink_to(cfg / "pappus", target_is_directory=True)
    new = datadir.adopt_legacy_root(home=tmp_path)
    assert new == cfg / "dandelion" and (new / "recall.json").is_file()
    for old in ("pappus", "solveit-sidekick"):                   # both old paths still work
        assert (cfg / old / "recall.json").read_text() == "{}"


def test_adopt_legacy_root_rekeys_papers_from_any_old_root(tmp_path, monkeypatch):
    from dandelion.paper import _md_key, _path_key
    monkeypatch.delenv("DANDELION_DATA", raising=False)
    monkeypatch.delenv("DANDELION_PAPERS", raising=False)
    cfg = tmp_path / ".config"
    papers = cfg / "pappus" / "papers"
    (papers / "uploads").mkdir(parents=True)
    for name in ("a.pdf", "b.pdf"):
        (papers / "uploads" / name).write_bytes(b"%PDF")
    def stamp(n):
        return (papers / "uploads" / n).stat().st_mtime_ns
    # a.pdf was converted under ~/.config/pappus, b.pdf back under solveit-sidekick
    key_a = _path_key(f"{cfg / 'pappus'}/papers/uploads/a.pdf", stamp("a.pdf"))
    key_b = _path_key(f"{cfg / 'solveit-sidekick'}/papers/uploads/b.pdf", stamp("b.pdf"))
    (papers / f"{key_a}.md").write_text("# A")
    (papers / f"{key_b}.md").write_text("# B")
    (papers / "sources.json").write_text(json.dumps({
        "d1": {"source": f"{cfg / 'solveit-sidekick'}/papers/uploads/b.pdf"},
        "d2": {"source": "https://arxiv.org/abs/1706.03762"}}))
    new = datadir.adopt_legacy_root(home=tmp_path)
    moved = new / "papers"
    assert (moved / f"{_md_key(moved / 'uploads' / 'a.pdf')}.md").read_text() == "# A"
    assert (moved / f"{_md_key(moved / 'uploads' / 'b.pdf')}.md").read_text() == "# B"
    src = json.loads((moved / "sources.json").read_text())
    assert src["d1"]["source"] == str(moved / "uploads" / "b.pdf")
    assert src["d2"]["source"] == "https://arxiv.org/abs/1706.03762"   # urls untouched


@pytest.mark.parametrize("legacy", datadir.LEGACY_ROOT_NAMES)
def test_adopt_legacy_root_never_overwrites(tmp_path, monkeypatch, legacy):
    monkeypatch.delenv("DANDELION_DATA", raising=False)
    cfg = tmp_path / ".config"
    (cfg / legacy).mkdir(parents=True)
    (cfg / "dandelion").mkdir()
    (cfg / "dandelion" / "keep").write_text("new")
    assert datadir.adopt_legacy_root(home=tmp_path) is None
    assert not (cfg / legacy).is_symlink()
    assert (cfg / "dandelion" / "keep").read_text() == "new"


def test_adopt_legacy_root_skipped_with_explicit_data_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("DANDELION_DATA", str(tmp_path / "elsewhere"))
    (tmp_path / ".config" / "pappus").mkdir(parents=True)
    assert datadir.adopt_legacy_root(home=tmp_path) is None
    assert not (tmp_path / ".config" / "dandelion").exists()


def test_legacy_env_vars_alias_to_dandelion(tmp_path):
    import subprocess, sys
    from pathlib import Path
    root = Path(__file__).resolve().parent.parent
    env = {k: v for k, v in os.environ.items()
           if not k.startswith(("DANDELION_", "PAPPUS_", "SIDEKICK_"))}
    env.update(SIDEKICK_TARGET="h100", SIDEKICK_PORT="9", SIDEKICK_HOST="a",
               PAPPUS_HOST="b", PAPPUS_PORT="7", DANDELION_PORT="8000", PYTHONPATH=str(root))
    code = ("import os, dandelion; e = os.environ; "
            "print(e['DANDELION_TARGET'], e['DANDELION_PORT'], e['DANDELION_HOST'])")
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True,
                         env=env, cwd=root, check=True).stdout.split()
    assert out == ["h100", "8000", "b"]       # explicit DANDELION_* wins, then PAPPUS_*, then SIDEKICK_*


# ---- rename migration hardening (review M1–M3, M5, L1–L5, L10) -------------------
def _old_root(tmp_path, name="pappus"):
    old = tmp_path / ".config" / name
    old.mkdir(parents=True)
    (old / "dialogs-kernel.json").write_text('{"a": []}')
    return old


def test_adopt_refuses_while_an_old_server_runs_there(tmp_path, monkeypatch):
    monkeypatch.delenv("DANDELION_DATA", raising=False)
    old = _old_root(tmp_path)
    (old / ".serve.pid").write_text(str(os.getppid()))            # alive, and not us
    with pytest.raises(datadir.LegacyAppRunning) as e:
        datadir.adopt_legacy_root(home=tmp_path)
    assert f"pid {os.getppid()}" in str(e.value) and "just stop" in str(e.value)
    assert not old.is_symlink() and not (tmp_path / ".config" / "dandelion").exists()
    (old / ".serve.pid").write_text(str(os.getpid()))             # our own pid: fine
    assert datadir.adopt_legacy_root(home=tmp_path) == tmp_path / ".config" / "dandelion"


def test_cli_prints_the_old_server_notice_and_keeps_going(tmp_path, monkeypatch, capsys):
    monkeypatch.delenv("DANDELION_DATA", raising=False)
    old = _old_root(tmp_path / "home")
    (old / ".serve.pid").write_text(str(os.getppid()))
    monkeypatch.setattr(datadir, "migrate_repo_data", lambda old=None: [])
    monkeypatch.setattr(datadir, "leftovers", lambda old=None: [])
    cli._move_out_of_repo()
    assert "An older Pappus server" in capsys.readouterr().err
    assert cli.main(["restore", str(tmp_path / "x.tar.gz")]) == 1   # would restore beside it
    assert cli.main(["backup", str(tmp_path)]) == 1                 # would back up nothing


@pytest.mark.parametrize("env", ["~/.config/dandelion", "~/.config/pappus",
                                 "~/.config/solveit-sidekick"])
def test_adopt_runs_when_the_data_env_names_a_default_root(tmp_path, monkeypatch, env):
    # e.g. PAPPUS_DATA=~/.config/pappus in a shell profile (aliased to DANDELION_DATA)
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("DANDELION_DATA", env)
    old = _old_root(tmp_path, env.rsplit("/", 1)[1].replace("dandelion", "pappus"))
    new = datadir.adopt_legacy_root()
    assert new == tmp_path / ".config" / "dandelion"
    assert (datadir.data_root() / "dialogs-kernel.json").read_text() == '{"a": []}'
    assert old.is_symlink()


def test_adopt_links_to_where_a_user_made_link_points(tmp_path, monkeypatch):
    monkeypatch.delenv("DANDELION_DATA", raising=False)
    cloud = tmp_path / "pCloud" / "pappus"
    cloud.mkdir(parents=True)
    (cloud / "recall.json").write_text("{}")
    cfg = tmp_path / ".config"
    cfg.mkdir()
    (cfg / "pappus").symlink_to(cloud, target_is_directory=True)
    (cfg / "solveit-sidekick").symlink_to(cfg / "pappus", target_is_directory=True)
    new = datadir.adopt_legacy_root(home=tmp_path)
    assert new == cfg / "dandelion" and new.is_symlink()
    assert new.resolve() == cloud.resolve() and (new / "recall.json").is_file()
    assert os.readlink(cfg / "pappus") == str(cloud)               # left as the user made it
    assert datadir.adopt_legacy_root(home=tmp_path) is None


def test_adopt_warns_when_both_roots_hold_data(tmp_path, monkeypatch):
    monkeypatch.delenv("DANDELION_DATA", raising=False)
    old = _old_root(tmp_path)
    new = tmp_path / ".config" / "dandelion"
    new.mkdir()
    (new / "dialogs-kernel.json").write_text("{}")
    with pytest.raises(datadir.MigrationNotice) as e:
        datadir.adopt_legacy_root(home=tmp_path)
    assert str(old) in str(e.value) and str(new) in str(e.value)
    assert not old.is_symlink() and (new / "dialogs-kernel.json").read_text() == "{}"


def test_cli_backup_moves_the_old_root_first(tmp_path, monkeypatch, capsys):
    monkeypatch.delenv("DANDELION_DATA", raising=False)
    monkeypatch.delenv("DANDELION_PAPERS", raising=False)
    _old_root(tmp_path / "home")
    assert cli.main(["backup", str(tmp_path)]) == 0
    assert "Moved your data to" in capsys.readouterr().out
    with tarfile.open(next(tmp_path.glob("dandelion-backup-*.tar.gz"))) as t:
        assert "data/dialogs-kernel.json" in t.getnames()


def test_secrets_and_tools_fall_back_to_an_old_root(tmp_path, monkeypatch):
    from dandelion import secrets_store, tools_config
    monkeypatch.delenv("DANDELION_SECRETS", raising=False)
    monkeypatch.delenv("DANDELION_TOOLS", raising=False)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("ZHIPU_API_KEY", raising=False)
    cfg = tmp_path / "home" / ".config"
    for name, key in (("solveit-sidekick", "sk-side"), ("pappus", "sk-pap")):
        (cfg / name).mkdir(parents=True)
        (cfg / name / "secrets.json").write_text(json.dumps({"ANTHROPIC_API_KEY": key}))
    (cfg / "solveit-sidekick" / "tools.json").write_text('{"allow": ["Mine"]}')
    assert secrets_store.get_key("ANTHROPIC_API_KEY") == "sk-pap"     # newest old root
    assert tools_config.allow_list() == ["Mine"]                      # any old root that has it
    secrets_store.save("ZHIPU_API_KEY", "z")                          # writes go to the new path
    new = cfg / "dandelion" / "secrets.json"
    assert json.loads(new.read_text()) == {"ANTHROPIC_API_KEY": "sk-pap", "ZHIPU_API_KEY": "z"}
    assert secrets_store.secrets_path() == new
    tools_config.save(["A"], [])
    assert (cfg / "dandelion" / "tools.json").is_file()
    assert json.loads((cfg / "solveit-sidekick" / "tools.json").read_text()) == {"allow": ["Mine"]}


def test_rekey_finds_sidekick_keys_under_a_symlinked_config(tmp_path, monkeypatch):
    # cache keys hash resolved paths: a paper converted while solveit-sidekick was
    # a real dir under a symlinked ~/.config carries realpath(~/.config) in its key
    from dandelion.paper import _md_key, _path_key
    monkeypatch.delenv("DANDELION_DATA", raising=False)
    monkeypatch.delenv("DANDELION_PAPERS", raising=False)
    real_cfg, home = tmp_path / "dotfiles" / "config", tmp_path / "me"
    home.mkdir()
    (real_cfg / "pappus" / "papers" / "uploads").mkdir(parents=True)
    (home / ".config").symlink_to(real_cfg, target_is_directory=True)
    papers = real_cfg / "pappus" / "papers"
    pdf = papers / "uploads" / "b.pdf"
    pdf.write_bytes(b"%PDF")
    key = _path_key(f"{real_cfg}/solveit-sidekick/papers/uploads/b.pdf", pdf.stat().st_mtime_ns)
    (papers / f"{key}.md").write_text("# B")
    new = datadir.adopt_legacy_root(home=home)
    moved = new / "papers" / "uploads" / "b.pdf"
    assert (new / "papers" / f"{_md_key(moved)}.md").read_text() == "# B"


def _old_papers(tmp_path, sub="papers"):
    """~/.config/pappus/<sub> holding b.pdf converted under the old path, and a
    sources.json pointing a dialog at it."""
    from dandelion.paper import _path_key
    cfg = tmp_path / ".config"
    papers = cfg / "pappus" / sub
    (papers / "uploads").mkdir(parents=True)
    pdf = papers / "uploads" / "b.pdf"
    pdf.write_bytes(b"%PDF")
    key = _path_key(f"{cfg / 'pappus'}/{sub}/uploads/b.pdf", pdf.stat().st_mtime_ns)
    (papers / f"{key}.md").write_text("# B")
    (papers / "sources.json").write_text(json.dumps({"d1": {"source": str(pdf)}}))
    return papers


def test_rekey_skips_an_unreadable_upload_and_still_repoints(tmp_path, monkeypatch):
    from dandelion.paper import _md_key
    monkeypatch.delenv("DANDELION_DATA", raising=False)
    monkeypatch.delenv("DANDELION_PAPERS", raising=False)
    papers = _old_papers(tmp_path)
    (papers / "uploads" / "0-gone.pdf").symlink_to(tmp_path / "nowhere.pdf")   # sorts first
    new = datadir.adopt_legacy_root(home=tmp_path)
    moved = new / "papers"
    assert (moved / f"{_md_key(moved / 'uploads' / 'b.pdf')}.md").read_text() == "# B"
    src = json.loads((moved / "sources.json").read_text())
    assert src["d1"]["source"] == str(moved / "uploads" / "b.pdf")


def test_rekey_reruns_on_every_start_after_a_failed_one(tmp_path, monkeypatch):
    from dandelion.paper import _md_key
    monkeypatch.delenv("DANDELION_DATA", raising=False)
    monkeypatch.delenv("DANDELION_PAPERS", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))
    _old_papers(tmp_path)
    real = datadir._rekey_moved_papers

    def boom(*a, **k):
        raise RuntimeError("crash mid re-key")
    monkeypatch.setattr(datadir, "_rekey_moved_papers", boom)
    new = datadir.adopt_legacy_root()                              # the move still completes
    assert new == tmp_path / ".config" / "dandelion"
    assert (tmp_path / ".config" / "pappus").is_symlink()
    monkeypatch.setattr(datadir, "_rekey_moved_papers", real)
    assert datadir.rekey_papers() == 1                             # the next start finishes it
    assert (new / "papers" / f"{_md_key(new / 'papers' / 'uploads' / 'b.pdf')}.md").is_file()
    assert datadir.rekey_papers() == 0                             # and is idempotent


def test_cli_adoption_failure_never_blocks_serve(tmp_path, monkeypatch, capsys):
    def boom(home=None):
        raise RuntimeError("unexpected")
    monkeypatch.setattr(datadir, "adopt_legacy_root", boom)
    monkeypatch.setattr(datadir, "rekey_papers", boom)
    monkeypatch.setattr(datadir, "migrate_repo_data", lambda old=None: [])
    monkeypatch.setattr(datadir, "leftovers", lambda old=None: [])
    cli._move_out_of_repo()
    err = capsys.readouterr().err
    assert "could not move the old data folder" in err and "could not re-key" in err


def test_adopt_reports_a_move_whose_link_failed(tmp_path, monkeypatch):
    from pathlib import Path

    from dandelion.paper import _md_key
    monkeypatch.delenv("DANDELION_DATA", raising=False)
    monkeypatch.delenv("DANDELION_PAPERS", raising=False)
    _old_papers(tmp_path)

    def no_link(self, *a, **k):
        raise PermissionError("read-only")
    monkeypatch.setattr(Path, "symlink_to", no_link)
    with pytest.raises(datadir.MigrationNotice) as e:
        datadir.adopt_legacy_root(home=tmp_path)
    old, new = tmp_path / ".config" / "pappus", tmp_path / ".config" / "dandelion"
    assert "could not leave a link at" in str(e.value) and str(old) in str(e.value)
    assert not old.exists() and new.is_dir()
    moved = new / "papers"
    assert (moved / f"{_md_key(moved / 'uploads' / 'b.pdf')}.md").read_text() == "# B"  # re-keyed anyway


def test_rekey_uses_a_papers_dir_by_another_name(tmp_path, monkeypatch):
    from dandelion.paper import _md_key
    monkeypatch.delenv("DANDELION_DATA", raising=False)
    monkeypatch.setenv("DANDELION_PAPERS", str(tmp_path / ".config" / "pappus" / "library"))
    _old_papers(tmp_path, sub="library")
    new = datadir.adopt_legacy_root(home=tmp_path)
    lib = new / "library"
    assert (lib / f"{_md_key(lib / 'uploads' / 'b.pdf')}.md").read_text() == "# B"
    assert json.loads((lib / "sources.json").read_text())["d1"]["source"] == str(lib / "uploads" / "b.pdf")


def test_restore_repoints_sources_from_another_machine(tmp_path, monkeypatch):
    a = tmp_path / "a"
    _seed(a)
    (a / "papers" / "sources.json").write_text(json.dumps({
        "d1": {"source": "/Users/someone/.config/pappus/papers/uploads/abc.pdf"},
        "d2": {"source": "/Users/someone/.config/pappus/papers/uploads/missing.pdf"},
        "d3": {"source": "https://arxiv.org/abs/1706.03762"}}))
    _env(monkeypatch, a)
    arc, _ = datadir.backup(tmp_path / "a.tar.gz")
    b = tmp_path / "b"
    _env(monkeypatch, b)
    datadir.restore(arc)
    src = json.loads((b / "papers" / "sources.json").read_text())
    assert src["d1"]["source"] == str(b / "papers" / "uploads" / "abc.pdf")
    assert src["d2"]["source"].startswith("/Users/someone/")     # no such upload: left alone
    assert src["d3"]["source"] == "https://arxiv.org/abs/1706.03762"


def test_page_head_copies_old_layout_keys():
    from fasthtml.common import to_xml

    from dandelion import app
    head = "".join(to_xml(h) for h in app._LOCAL_HDRS)
    assert "'pappus_'" in head and "'dandelion_'+k.slice(7)" in head
    assert head.index("pappus_") < head.index("htmx.min.js")     # before any script reads them


def test_demo_data_refuses_real_data(tmp_path):
    import subprocess
    import sys
    from pathlib import Path
    script = Path(__file__).resolve().parent.parent / "scripts" / "demo_data.py"
    home = tmp_path / "h"
    (tmp_path / "realcfg").mkdir()
    home.mkdir()
    (home / ".config").symlink_to(tmp_path / "realcfg", target_is_directory=True)
    env = {k: v for k, v in os.environ.items()
           if not k.startswith(("DANDELION_", "PAPPUS_", "SIDEKICK_"))}
    env["HOME"] = str(home)

    def run(*args):
        return subprocess.run([sys.executable, str(script), *map(str, args)],
                              capture_output=True, text=True, env=env, cwd=tmp_path, check=False)
    r = run(tmp_path / "realcfg" / "x")                            # ~/.config by its real path
    assert r.returncode != 0 and "~/.config" in r.stderr
    mine = tmp_path / "notes"
    (mine / "data").mkdir(parents=True)
    (mine / "data" / "dialogs-kernel.json").write_text('{"real": []}')
    r = run(mine)
    assert r.returncode != 0 and "--force" in r.stderr
    assert (mine / "data" / "dialogs-kernel.json").read_text() == '{"real": []}'
