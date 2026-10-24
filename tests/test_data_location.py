"""Choosing the data folder: precedence, the pointer file, and the marker guard
that refuses an unmarked (e.g. unmounted) folder without writing to it."""
import json
import tarfile

import pytest
from starlette.testclient import TestClient

from dandelion import client, datadir, recall


@pytest.fixture
def home(tmp_path, monkeypatch):
    """The conftest HOME, with no DANDELION_DATA so the pointer/default apply."""
    monkeypatch.delenv("DANDELION_DATA", raising=False)
    h = tmp_path / "home"
    h.mkdir(exist_ok=True)
    return h


def _pointer(home):
    return home / ".config" / "dandelion" / "data-root"


def _write_pointer(home, target):
    _pointer(home).parent.mkdir(parents=True, exist_ok=True)
    _pointer(home).write_text(str(target) + "\n")


def test_precedence_env_then_pointer_then_default(home, tmp_path, monkeypatch):
    assert datadir.data_root() == home / ".config" / "dandelion"
    assert datadir.data_root_source() == "default"
    _write_pointer(home, tmp_path / "ptr")
    assert (datadir.data_root(), datadir.data_root_source()) == (tmp_path / "ptr", "pointer")
    monkeypatch.setenv("DANDELION_DATA", str(tmp_path / "env"))
    assert (datadir.data_root(), datadir.data_root_source()) == (tmp_path / "env", "env")


def test_pointer_must_be_an_absolute_path(home):
    _pointer(home).parent.mkdir(parents=True)
    _pointer(home).write_text("relative/dir")
    assert datadir.read_pointer() is None
    assert "data-root setting is invalid" in datadir.store_refusal()   # never a silent fallback
    _pointer(home).write_text("~/x")
    assert datadir.read_pointer() == home / "x"          # ~ expands


def test_use_folder_sets_marker_and_pointer_and_clear_forgets_it(home, tmp_path):
    (tmp_path / "new").mkdir()
    (tmp_path / "new" / "other").write_text("x")         # a live drive: parent is not empty
    ok, msg = datadir.use_folder(str(tmp_path / "new" / "store"))
    assert ok and "restart" in msg and "Nothing was moved" in msg
    assert (tmp_path / "new" / "store" / datadir.MARKER).is_file()
    assert datadir.read_pointer() == tmp_path / "new" / "store"
    ok, _ = datadir.clear_pointer()
    assert ok and datadir.read_pointer() is None
    assert (tmp_path / "new" / "store" / datadir.MARKER).is_file()   # the folder is left alone


def test_use_folder_rejects_relative_and_file_paths(home, tmp_path):
    assert not datadir.use_folder("relative")[0]
    assert not datadir.use_folder("")[0]
    f = tmp_path / "afile"
    f.write_text("x")
    assert not datadir.use_folder(str(f))[0]
    assert datadir.read_pointer() is None


def test_non_empty_unmarked_folder_needs_confirmation(home, tmp_path):
    d = tmp_path / "docs"
    d.mkdir()
    (d / "notes.txt").write_text("mine")
    ok, msg = datadir.use_folder(str(d))
    assert not ok and datadir.MARKER in msg
    assert not (d / datadir.MARKER).exists() and datadir.read_pointer() is None
    assert datadir.use_folder(str(d), confirm=True)[0]
    assert (d / datadir.MARKER).is_file() and datadir.read_pointer() == d
    assert (d / "notes.txt").read_text() == "mine"


def test_marked_non_empty_folder_needs_no_confirmation(home, tmp_path):
    d = tmp_path / "store"
    d.mkdir()
    (d / datadir.MARKER).write_text("")
    (d / "dialogs-kernel.json").write_text("{}")
    assert datadir.use_folder(str(d))[0]


def test_unmarked_root_is_refused_and_nothing_is_written(tmp_path, monkeypatch):
    empty = tmp_path / "pcloud-unmounted"
    empty.mkdir()
    monkeypatch.setenv("DANDELION_DATA", str(empty))
    assert "no .dandelion-store marker" in datadir.store_refusal()
    client._save_dialogs("kernel", {"d": [client.Msg(id="1", msg_type="note", content="hi")]})
    client._write_private_dialogs("kernel", {"d"})
    recall.save_schedule({"a": 1})
    datadir.mark_serving()
    assert datadir.rekey_papers() == 0
    assert list(empty.iterdir()) == []                   # no store, backups/, .serve.pid, marker
    with pytest.raises(datadir.StoreRefused):
        datadir.restore(tmp_path / "x.tar.gz")
    assert list(empty.iterdir()) == []


def test_missing_unmarked_root_is_not_created(tmp_path, monkeypatch):
    gone = tmp_path / "Volumes" / "pcloud"
    monkeypatch.setenv("DANDELION_DATA", str(gone))
    client._save_dialogs("kernel", {"d": []})
    datadir.mark_serving()
    assert not gone.exists() and not (tmp_path / "Volumes").exists()


def test_refused_root_serves_a_banner_and_settings_still_loads(tmp_path, monkeypatch):
    from dandelion import app
    empty = tmp_path / "unmounted"
    empty.mkdir()
    monkeypatch.setenv("DANDELION_DATA", str(empty))
    tc = TestClient(app.app)
    assert "has no .dandelion-store marker" in tc.get("/").text
    s = tc.get("/settings")
    assert s.status_code == 200 and "Data folder" in s.text and "unmounted" in s.text
    assert list(empty.iterdir()) == []


def test_marked_root_opens_and_saves(tmp_path, monkeypatch):
    d = tmp_path / "store"
    d.mkdir()
    (d / datadir.MARKER).write_text("")
    monkeypatch.setenv("DANDELION_DATA", str(d))
    assert datadir.store_refusal() is None
    client._save_dialogs("kernel", {"d": [client.Msg(id="1", msg_type="note", content="hi")]})
    assert json.loads((d / "dialogs-kernel.json").read_text())["d"][0]["content"] == "hi"


def test_default_root_is_never_refused_and_gets_the_marker(home):
    root = home / ".config" / "dandelion"
    assert datadir.store_refusal() is None               # even before the marker exists
    datadir.claim_default_root()
    assert (root / datadir.MARKER).is_file()
    client._save_dialogs("kernel", {"d": []})
    assert (root / "dialogs-kernel.json").is_file()


def test_claim_leaves_a_pointed_root_alone(home, tmp_path):
    _write_pointer(home, tmp_path / "elsewhere")
    datadir.claim_default_root()
    assert not (home / ".config" / "dandelion" / datadir.MARKER).exists()
    assert not (tmp_path / "elsewhere").exists()


def test_the_marker_is_not_carried_in_a_backup(tmp_path, monkeypatch):
    d = tmp_path / "store"
    d.mkdir()
    (d / datadir.MARKER).write_text("")
    (d / "recall.json").write_text("{}")
    monkeypatch.setenv("DANDELION_DATA", str(d))
    arc, _ = datadir.backup(tmp_path / "b.tar.gz")
    with tarfile.open(arc) as t:
        assert not any(n.endswith(datadir.MARKER) for n in t.getnames())


# ---- Settings ----------------------------------------------------------------
def test_settings_post_sets_and_clears_the_pointer_without_moving_data(home, tmp_path):
    from dandelion import app
    root = home / ".config" / "dandelion"
    root.mkdir(parents=True)
    (root / "dialogs-kernel.json").write_text('{"d": []}')
    tc = TestClient(app.app)
    target = tmp_path / "pcloud" / "dandelion"
    (tmp_path / "pcloud").mkdir()
    r = tc.post("/settings/data-folder", data={"action": "use", "path": str(target),
                                               "confirm": "on"})
    assert "restart" in r.text and datadir.read_pointer() == target
    assert (target / datadir.MARKER).is_file()
    assert list(target.iterdir()) == [target / datadir.MARKER]           # nothing copied in
    assert (root / "dialogs-kernel.json").read_text() == '{"d": []}'     # nothing moved out
    r = tc.post("/settings/data-folder", data={"action": "clear"})
    assert datadir.read_pointer() is None and "Back to the default" in r.text
    assert (root / "dialogs-kernel.json").read_text() == '{"d": []}'


def test_settings_post_asks_before_marking_a_non_empty_folder(home, tmp_path):
    from dandelion import app
    d = tmp_path / "docs"
    d.mkdir()
    (d / "a.txt").write_text("x")
    tc = TestClient(app.app)
    r = tc.post("/settings/data-folder", data={"action": "use", "path": str(d)})
    assert "tick the confirmation" in r.text and datadir.read_pointer() is None
    tc.post("/settings/data-folder", data={"action": "use", "path": str(d), "confirm": "on"})
    assert datadir.read_pointer() == d


def test_settings_shows_the_env_var_as_overriding(tmp_path):
    from dandelion import app
    tc = TestClient(app.app)                              # conftest sets DANDELION_DATA
    html = tc.get("/settings").text
    assert "DANDELION_DATA environment variable" in html and "disabled" in html
    r = tc.post("/settings/data-folder", data={"action": "use", "path": str(tmp_path / "z")})
    assert "overrides" in r.text and datadir.read_pointer() is None
    assert not (tmp_path / "z").exists()


# ---- keys, tool policy and the pointer stay in ~/.config/dandelion -----------
def test_keys_tools_and_pointer_stay_out_of_the_data_folder(home, tmp_path, monkeypatch):
    from dandelion import secrets_store, tools_config
    for v in ("DANDELION_SECRETS", "DANDELION_TOOLS"):
        monkeypatch.delenv(v)
    data = tmp_path / "pcloud-store"
    assert datadir.use_folder(str(data))[0]
    monkeypatch.setenv("DANDELION_PAPERS", str(data / "papers"))
    cfg = home / ".config" / "dandelion"
    assert datadir.data_root() == data
    assert secrets_store.secrets_path(write=True) == cfg / "secrets.json"
    assert tools_config.tools_path(write=True) == cfg / "tools.json"
    assert _pointer(home).parent == cfg
    secrets_store.save("ANTHROPIC_API_KEY", "sk-test")
    tools_config.save(["WebSearch"], [])
    client._save_dialogs("kernel", {"d": [client.Msg(id="1", msg_type="note", content="hi")]})
    names = {p.name for p in data.rglob("*")}
    assert "secrets.json" not in names and "tools.json" not in names
    assert (cfg / "secrets.json").is_file() and (cfg / "tools.json").is_file()
    arc, _ = datadir.backup(tmp_path / "b.tar.gz")
    with tarfile.open(arc) as t:
        assert "data/dialogs-kernel.json" in t.getnames()
        assert not [n for n in t.getnames() if "secrets" in n]
        assert "sk-test" not in "".join(t.extractfile(m).read().decode("utf-8", "ignore")
                                        for m in t.getmembers() if m.isfile())


# ---- review fixes ------------------------------------------------------------
def _msg(i="1", text="hi"):
    return {"d": [client.Msg(id=i, msg_type="note", content=text)]}


def _real_store(d, text="REMOTE-REAL-WORK"):
    d.mkdir(exist_ok=True)
    (d / datadir.MARKER).write_text("")
    (d / "dialogs-kernel.json").write_text(json.dumps(
        {"d": [{"id": "r", "msg_type": "note", "content": text}]}))


def test_b1_a_pointer_change_does_not_redirect_saves_until_restart(home, tmp_path):
    root = home / ".config" / "dandelion"
    datadir.claim_default_root()
    datadir.freeze_root()
    client._save_dialogs("kernel", _msg(text="local-old"))
    other = tmp_path / "pcloud"
    _real_store(other)
    assert datadir.use_folder(str(other))[0]
    assert datadir.data_root() == root and datadir.pending_root() == other
    client._save_dialogs("kernel", _msg(text="local-new"))
    assert "REMOTE-REAL-WORK" in (other / "dialogs-kernel.json").read_text()   # intact
    assert "local-new" in (root / "dialogs-kernel.json").read_text()
    datadir.unfreeze_root()
    assert datadir.data_root() == other                  # a restart picks it up


def test_b1_serve_pid_lands_in_the_frozen_root(home, tmp_path):
    datadir.freeze_root()
    root = datadir.data_root()
    datadir.use_folder(str(_mk(tmp_path / "x")))
    datadir.mark_serving()
    assert (root / ".serve.pid").is_file()


def _mk(p):
    p.mkdir()
    (p / datadir.MARKER).write_text("")
    return p


def test_b2_a_refusal_is_latched_until_restart(tmp_path, monkeypatch):
    d = tmp_path / "pcloud"
    d.mkdir()
    monkeypatch.setenv("DANDELION_DATA", str(d))
    datadir.freeze_root()
    client._save_dialogs("kernel", _msg(text="typed-while-unmounted"))
    _real_store(d)                                       # the drive mounts: marker + real store
    assert datadir.store_refusal()                       # still refused
    client._save_dialogs("kernel", _msg(text="typed-while-unmounted"))
    assert "REMOTE-REAL-WORK" in (d / "dialogs-kernel.json").read_text()
    datadir.mark_serving()
    assert not (d / ".serve.pid").exists()


def test_frozen_root_stops_writing_when_the_mount_goes_away(tmp_path, monkeypatch):
    d = _mk(tmp_path / "pcloud")
    monkeypatch.setenv("DANDELION_DATA", str(d))
    datadir.freeze_root()
    assert datadir.store_refusal() is None
    (d / datadir.MARKER).unlink()
    d.rmdir()
    client._save_dialogs("kernel", _msg())
    assert not d.exists()                                # never recreated


def test_b3_blog_and_exports_never_write_into_a_refused_root(tmp_path, monkeypatch):
    from dandelion import app, blog, nbdev_export
    gone = tmp_path / "Volumes" / "pcloud"
    monkeypatch.setenv("DANDELION_DATA", str(gone))
    with pytest.raises(datadir.StoreRefused):
        blog.publish_dialog(app.STATE["backend"], "demo/welcome", blog.default_blog_dir())
    with pytest.raises(datadir.StoreRefused):
        blog.build_blog(app.STATE["backend"], ["demo/welcome"], blog.default_blog_dir())
    with pytest.raises(datadir.StoreRefused):
        nbdev_export.write_files(str(gone / "lib"), {"a.txt": "x"})
    r = TestClient(app.app).get("/publish/blog", follow_redirects=False)
    assert r.status_code == 303                          # flashed, not a 500
    assert not (tmp_path / "Volumes").exists()


def test_n3_upload_into_a_refused_root_flashes(tmp_path, monkeypatch):
    from dandelion import app
    d = tmp_path / "unmounted"
    d.mkdir()
    monkeypatch.setenv("DANDELION_DATA", str(d))
    monkeypatch.delenv("DANDELION_PAPERS")
    r = TestClient(app.app).post("/paper/open", files={"pdf": ("a.pdf", b"%PDF-1")})
    assert r.status_code == 200 and list(d.iterdir()) == []


def test_s1_empty_folder_and_empty_parent_need_confirmation(home, tmp_path):
    empty = tmp_path / "mountpoint"
    empty.mkdir()
    ok, msg = datadir.use_folder(str(empty))
    assert not ok and "Is your cloud drive running?" in msg and list(empty.iterdir()) == []
    ok, msg = datadir.use_folder(str(empty / "dandelion"))          # parent is empty
    assert not ok and "Is your cloud drive running?" in msg and list(empty.iterdir()) == []
    assert datadir.use_folder(str(empty), confirm=True)[0]
    assert (empty / datadir.MARKER).is_file()


def test_s1_the_currently_refused_root_needs_confirmation_and_parent_must_exist(
        home, tmp_path, monkeypatch):
    d = tmp_path / "pc"
    d.mkdir()
    (d / "x").write_text("")
    r = d / "dandelion"
    monkeypatch.setenv("DANDELION_DATA", str(r))
    ok, msg = datadir.use_folder(str(r))
    assert not ok and "cloud drive" in msg and not r.exists()
    ok, msg = datadir.use_folder(str(tmp_path / "nope" / "deep"), confirm=True)
    assert not ok and "does not exist" in msg and not (tmp_path / "nope").exists()


def test_s2_an_unreadable_root_renders_instead_of_500(tmp_path, monkeypatch):
    from dandelion import app
    d = tmp_path / "locked"
    d.mkdir()
    d.chmod(0)
    try:
        monkeypatch.setenv("DANDELION_DATA", str(d))
        assert "Cannot access" in datadir.store_refusal()
        tc = TestClient(app.app)
        assert tc.get("/").status_code == 200 and tc.get("/settings").status_code == 200
    finally:
        d.chmod(0o755)


def test_s3_an_existing_unmarked_store_is_grandfathered(tmp_path, monkeypatch, capsys):
    d = tmp_path / "old"
    d.mkdir()
    (d / "dialogs-kernel.json").write_text('{"d": []}')
    monkeypatch.setenv("DANDELION_DATA", str(d))
    assert datadir.store_refusal()                       # until startup decides
    datadir.freeze_root()
    assert datadir.store_refusal() is None and (d / datadir.MARKER).is_file()
    assert "marked existing store" in capsys.readouterr().err


def test_s3_env_refusal_says_how_to_fix_it(tmp_path, monkeypatch, capsys):
    d = tmp_path / "empty"
    d.mkdir()
    monkeypatch.setenv("DANDELION_DATA", str(d))
    datadir.freeze_root()
    assert f"touch '{d}/.dandelion-store' or unset DANDELION_DATA" in datadir.store_refusal()
    assert "no .dandelion-store marker" in capsys.readouterr().err       # printed at start
    client._save_dialogs("kernel", _msg())
    client._save_dialogs("kernel", _msg())
    assert capsys.readouterr().err.count("not saving") == 1             # logged once


def test_s4_a_symlinked_default_root_is_not_exempt(home, tmp_path):
    mount = tmp_path / "pcloud-empty"
    mount.mkdir()
    (home / ".config").mkdir(parents=True)
    (home / ".config" / "dandelion").symlink_to(mount, target_is_directory=True)
    msg = datadir.store_refusal()
    assert msg and "is a link" in msg
    datadir.claim_default_root()
    client._save_dialogs("kernel", _msg())
    assert list(mount.iterdir()) == []
    (mount / "dialogs-kernel.json").write_text('{"d": []}')     # a real store behind the link
    datadir.freeze_root()
    assert datadir.store_refusal() is None


def test_s5_an_invalid_pointer_is_refused_not_ignored(home):
    for bad in ("/a\n/b\n", "relative", "\n", ""):
        _pointer(home).parent.mkdir(parents=True, exist_ok=True)
        _pointer(home).write_text(bad)
        assert "data-root setting is invalid" in datadir.store_refusal(), bad
        assert str(_pointer(home)) in datadir.store_refusal()
    client._save_dialogs("kernel", _msg())
    assert not (home / ".config" / "dandelion" / "dialogs-kernel.json").exists()


def test_s6_dangerous_targets_are_refused(home, tmp_path):
    from pathlib import Path
    for bad in ("/", str(home), "~", str(home / ".config"), str(datadir.config_dir()),
                str(datadir.REPO_DATA), str(datadir.REPO_DATA.parent)):
        assert not datadir.use_folder(bad, confirm=True)[0], bad
    assert datadir.read_pointer() is None
    _pointer(home).parent.mkdir(parents=True)
    _pointer(home).write_text("/")
    assert datadir.read_pointer() is None and datadir.store_refusal()
    assert Path("/") not in [datadir.data_root()]


def test_s7_backup_on_a_refused_root_exits_nonzero(tmp_path, monkeypatch, capsys):
    from dandelion import cli
    d = tmp_path / "unmounted"
    d.mkdir()
    monkeypatch.setenv("DANDELION_DATA", str(d))
    monkeypatch.chdir(tmp_path)
    assert cli.cmd_backup([str(tmp_path / "b.tar.gz")]) == 1
    assert "backup refused" in capsys.readouterr().err
    assert not (tmp_path / "b.tar.gz").exists()


# ---- re-review fixes -----------------------------------------------------------
def test_sf1_case_variants_of_home_and_repo_are_refused(home, tmp_path, monkeypatch):
    fake_repo = tmp_path / "Checkout" / "app"
    (fake_repo / "data").mkdir(parents=True)
    monkeypatch.setattr(datadir, "REPO_DATA", fake_repo / "data")
    if not (tmp_path / "CHECKOUT").exists():
        pytest.skip("case-sensitive filesystem")
    home.mkdir(exist_ok=True)
    for bad in (str(home).upper() if (tmp_path / "HOME").exists() else str(home),
                str(tmp_path / "CHECKOUT" / "APP"), str(tmp_path / "checkout" / "APP" / "sub")):
        assert not datadir.use_folder(bad, confirm=True)[0], bad
    assert not (fake_repo / datadir.MARKER).exists()
    assert datadir.read_pointer() is None


def test_sf2_the_message_names_the_old_root_to_back_up(home, tmp_path):
    d = _mk(tmp_path / "new")
    ok, msg = datadir.use_folder(str(d))
    assert ok and f"DANDELION_DATA='{home / '.config' / 'dandelion'}' dandelion backup" in msg


def test_sf3_make_dirs_never_creates_a_non_default_root(tmp_path, monkeypatch):
    gone = tmp_path / "a" / "pcloud"
    monkeypatch.setenv("DANDELION_DATA", str(gone))
    datadir.make_dirs(gone)
    with pytest.raises(OSError):
        datadir.make_dirs(gone / "backups")
    assert not (tmp_path / "a").exists()


def _env_root(tmp_path, monkeypatch, name="old"):
    d = tmp_path / name
    d.mkdir()
    monkeypatch.setenv("DANDELION_DATA", str(d))
    return d


def test_sf4_only_a_readable_non_empty_store_is_grandfathered(tmp_path, monkeypatch):
    d = _env_root(tmp_path, monkeypatch)
    (d / "dialogs-a.json").write_text("")                          # 0 bytes
    (d / "dialogs-b.json").mkdir()                                 # a directory
    (d / "dialogs-c.local.json").write_text('{"d": []}')           # local alone
    (d / "dialogs-e.json").write_text("{}")                        # no dialog
    datadir.freeze_root()
    assert datadir.store_refusal() and not (d / datadir.MARKER).exists()
    datadir.unfreeze_root()
    (d / "dialogs-kernel.json").write_text('{"d": []}')
    datadir.freeze_root()
    assert datadir.store_refusal() is None and (d / datadir.MARKER).exists()


def test_sf4_a_shadow_store_in_a_pointer_root_stays_refused(home, tmp_path):
    shadow = tmp_path / "mountpoint"
    shadow.mkdir()
    (shadow / "dialogs-kernel.json").write_text('{"d": []}')
    _write_pointer(home, shadow)
    datadir.freeze_root()
    assert datadir.store_refusal() and not (shadow / datadir.MARKER).exists()


def test_sf5_serve_freezes_the_root_before_uvicorn_runs(tmp_path, monkeypatch):
    import uvicorn

    from dandelion import cli
    seen = []
    monkeypatch.setattr(uvicorn, "run", lambda *a, **k: seen.append(datadir._FROZEN))
    monkeypatch.setattr(cli, "_move_out_of_repo", lambda: True)
    monkeypatch.delenv("DANDELION_RELOAD", raising=False)
    monkeypatch.delenv("DANDELION_HOST", raising=False)
    assert cli.cmd_serve([]) == 0
    assert seen and seen[0] is not None and seen[0]["root"] == tmp_path


def test_sf5_the_startup_hook_freezes_the_root(tmp_path, monkeypatch):
    from dandelion import app
    monkeypatch.setattr(app, "use_target", lambda name: None)
    monkeypatch.setattr(app, "_initial_target", lambda: "kernel")
    app.STATE.pop("_target_ready", None)
    assert datadir._FROZEN is None
    app._ensure_target()
    assert datadir._FROZEN["root"] == tmp_path
