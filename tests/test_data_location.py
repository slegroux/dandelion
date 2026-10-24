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
    assert datadir.read_pointer() is None and datadir.data_root_source() == "default"
    _pointer(home).write_text("~/x")
    assert datadir.read_pointer() == home / "x"          # ~ expands


def test_use_folder_sets_marker_and_pointer_and_clear_forgets_it(home, tmp_path):
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
    r = tc.post("/settings/data-folder", data={"action": "use", "path": str(target)})
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
