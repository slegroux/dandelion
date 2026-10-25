"""One file per dialog: a sync conflict can only ever touch one dialog."""
import json
import os

import pytest

from dandelion import cli, client, datadir
from dandelion.client import Msg, _save_dialogs

KEY = "kernel"


@pytest.fixture(autouse=True)
def _fresh(monkeypatch):
    monkeypatch.setattr(client, "STORE_NOTICES", [])
    monkeypatch.setattr(client, "SNAPSHOT_MINUTES", 0)
    monkeypatch.setattr(client, "SNAPSHOT_KEEP", 5)


def _d(local=False):
    return client._dir_path(KEY, local)


def _f(name, local=False):
    return client._dialog_file(_d(local), name)


def _dialogs(tag="x", names=("zeta", "Alpha", "mid")):
    return {n: [Msg(f"{n}1", "note", f"{tag}-{n}")] for n in names}


def _files(local=False):
    return sorted(f.name for f in _d(local).glob("*.json") if f.name != client._INDEX)


def _text(dialogs):
    return {n: [m.content for m in v] for n, v in dialogs.items()}


def _legacy(root, raw):
    p = root / "dialogs-kernel.json"
    p.write_text(json.dumps(raw, indent=2))
    return p


RAW = {
    "zeta": [{"id": "a", "msg_type": "note", "content": "z", "extra": 1}],
    "Alpha/sub": [{"id": "b", "msg_type": "code", "content": "1+1", "output": "2"}],
    "alpha/sub": [],
    "dialog: with ? odd *chars*": [{"id": "c", "msg_type": "note", "content": "é"}],
}


def test_old_to_new_to_old_is_the_same_json(tmp_path):
    p = _legacy(tmp_path, RAW)
    (tmp_path / "dialogs-kernel.local.json").write_text(json.dumps({"priv": []}))
    assert client.split_store(KEY)
    assert not p.exists() and p.with_name(p.name + ".pre-split").is_file()
    assert client._read_dir(_d()) == RAW and list(client._read_dir(_d())) == list(RAW)
    assert client.join_store(KEY)
    assert json.loads(p.read_text()) == RAW and list(json.loads(p.read_text())) == list(RAW)
    assert json.loads((tmp_path / "dialogs-kernel.local.json").read_text()) == {"priv": []}
    assert not _d().exists() and (tmp_path / "dialogs-kernel.d.pre-join").is_dir()
    assert client.split_store(KEY)                       # and again: nothing is overwritten
    assert client._read_dir(_d()) == RAW
    assert len(list(tmp_path.glob("dialogs-kernel.json.pre-split*"))) == 2


def test_names_that_differ_only_in_case_get_different_files(tmp_path):
    assert client._dialog_file(_d(), "Alpha").name.lower() != client._dialog_file(_d(), "alpha").name.lower()
    _save_dialogs(KEY, _dialogs(names=("Alpha", "alpha", "a/b", "a_b")))
    assert len({n.lower() for n in _files()}) == 4
    assert sorted(client._load_dialogs(KEY)) == ["Alpha", "a/b", "a_b", "alpha"]


def test_saving_one_edited_dialog_rewrites_exactly_one_file(tmp_path):
    dialogs = _dialogs()
    _save_dialogs(KEY, dialogs)
    assert len(list(_d().glob("*.json"))) == 4            # three dialogs and the index
    for f in _d().glob("*.json"):
        os.utime(f, ns=(1, 1))
    loaded = client._load_dialogs(KEY)
    loaded["mid"].append(Msg("m2", "note", "edited"))
    _save_dialogs(KEY, loaded)
    changed = [f.name for f in _d().glob("*.json") if f.stat().st_mtime_ns != 1]
    assert changed == [_f("mid").name]
    assert [m.content for m in client._load_dialogs(KEY)["mid"]] == ["x-mid", "edited"]


def test_order_survives_and_a_lost_index_degrades_to_mtime(tmp_path):
    names = ("zeta", "Alpha", "mid")
    _save_dialogs(KEY, _dialogs(names=names))
    assert list(client._load_dialogs(KEY)) == list(names)
    (_d() / client._INDEX).write_text("{garbage")
    (_d() / f"{client._INDEX}.bak").unlink(missing_ok=True)
    for i, n in enumerate(("mid", "zeta", "Alpha")):
        os.utime(_f(n), ns=(10 ** 9 * (i + 1),) * 2)
    assert list(client._load_dialogs(KEY)) == ["mid", "zeta", "Alpha"]   # nothing lost
    (_d() / client._INDEX).unlink()
    assert sorted(client._load_dialogs(KEY)) == sorted(names)


def test_two_stores_edit_different_dialogs_and_both_survive(tmp_path):
    _save_dialogs(KEY, _dialogs())
    a, b = client._load_dialogs(KEY), client._load_dialogs(KEY)    # two Macs, same start
    a["zeta"].append(Msg("a2", "note", "from A"))
    _save_dialogs(KEY, a)
    client._DIGESTS[str(_d())] = {n: client._digest(client._dialog_body(     # B's own memory
        n, [m.__dict__ for m in b[n]])) for n in b}
    b["mid"].append(Msg("b2", "note", "from B"))
    _save_dialogs(KEY, b)
    got = _text(client._load_dialogs(KEY))
    assert got["zeta"] == ["x-zeta", "from A"] and got["mid"] == ["x-mid", "from B"]


def test_a_conflicted_copy_is_skipped_and_touches_nothing(tmp_path):
    _save_dialogs(KEY, _dialogs())
    f = _f("mid")
    stray = f.with_name(f.name[:-5] + " (conflicted copy 2026-10-25).json")
    stray.write_text(f.read_text())
    loaded = client._load_dialogs(KEY)
    assert sorted(loaded) == ["Alpha", "mid", "zeta"]
    assert any("conflicted copy" in n for n in client.STORE_NOTICES)
    _save_dialogs(KEY, loaded)
    assert stray.exists()                                  # left where it is


def test_a_damaged_file_affects_one_dialog_only(tmp_path):
    _save_dialogs(KEY, _dialogs())
    _f("Alpha").write_text("{torn")
    for b in [_f("Alpha").with_suffix(".json.bak"), *client._snapshots(_f("Alpha"))]:
        b.unlink(missing_ok=True)
    loaded = client._load_dialogs(KEY)
    assert sorted(loaded) == ["mid", "zeta"]
    assert list(_d().glob("Alpha*.corrupt-*")) == [] and len(list(_d().glob("*.corrupt-*"))) == 1
    _save_dialogs(KEY, loaded)
    assert len(list(_d().glob("*.corrupt-*"))) == 1       # still kept


def test_a_deleted_or_renamed_dialog_loses_its_file_but_is_snapshotted(tmp_path):
    _save_dialogs(KEY, _dialogs())
    loaded = client._load_dialogs(KEY)
    loaded["new name"] = loaded.pop("mid")
    del loaded["zeta"]
    _save_dialogs(KEY, loaded)
    assert not _f("mid").exists() and not _f("zeta").exists()
    assert not _f("zeta").with_suffix(".json.bak").exists()
    assert len(client._snapshots(_f("zeta"))) == 1
    assert sorted(client._load_dialogs(KEY)) == ["Alpha", "new name"]


def test_private_dialogs_stay_in_the_local_folder(tmp_path):
    (tmp_path / "dialogs-kernel.private").write_text("secret\n")
    _save_dialogs(KEY, _dialogs(names=("pub", "secret")))
    assert _files() == [_f("pub").name] and _files(True) == [_f("secret", True).name]
    assert client._load_dialogs(KEY)["secret"][0].content == "x-secret"
    (tmp_path / "dialogs-kernel.private").write_text("")      # made public again
    _save_dialogs(KEY, client._load_dialogs(KEY))
    assert sorted(_files()) == sorted([_f("pub").name, _f("secret").name])
    assert not _d(True).exists()


def test_the_first_load_imports_the_old_file_and_keeps_it(tmp_path):
    p = _legacy(tmp_path, {"d": [{"id": "1", "msg_type": "note", "content": "old"}]})
    loaded = client._load_dialogs(KEY)
    assert loaded["d"][0].content == "old"
    assert _d().is_dir() and not p.exists() and p.with_name(p.name + ".pre-split").is_file()
    assert [m.content for m in client._load_dialogs(KEY)["d"]] == ["old"]


def test_a_first_save_of_an_unsplit_store_keeps_the_old_file(tmp_path, monkeypatch):
    p = _legacy(tmp_path, {"d": [{"id": "1", "msg_type": "note", "content": "old"}]})
    with monkeypatch.context() as m:
        m.setattr(datadir, "_in_use_refusal", lambda: "in use elsewhere")
        loaded = client._load_dialogs(KEY)                  # read-only: read as it is
    assert p.exists() and not _d().exists() and loaded["d"][0].content == "old"
    loaded["d"].append(Msg("2", "note", "new"))
    _save_dialogs(KEY, loaded)
    assert _d().is_dir() and p.with_name(p.name + ".pre-split").is_file()
    assert json.loads(p.with_name(p.name + ".pre-split").read_text())["d"][0]["content"] == "old"


def test_a_refused_root_writes_nothing(tmp_path, monkeypatch):
    p = _legacy(tmp_path, {"d": []})
    before = sorted(x.name for x in tmp_path.rglob("*"))
    (tmp_path / datadir.MARKER).unlink()                    # no marker: refused
    monkeypatch.setattr(datadir, "_is_default", lambda r: False)
    with pytest.raises(datadir.StoreRefused):
        client.split_store(KEY)
    _save_dialogs(KEY, _dialogs())
    client._load_dialogs(KEY)
    (tmp_path / datadir.MARKER).write_text("")
    assert sorted(x.name for x in tmp_path.rglob("*")) == before and p.exists()


def test_a_missing_dialog_file_beside_its_bak_is_refused_not_dropped(tmp_path):
    _save_dialogs(KEY, _dialogs(tag="a"))
    _save_dialogs(KEY, _dialogs(tag="b"))                   # .bak files now exist
    _f("mid").unlink()                                      # a sync in progress
    with pytest.raises(client.StoreUnreadable):
        client._load_dialogs(KEY)
    _d().rename(tmp_path / "gone")                          # the whole folder
    with pytest.raises(client.StoreUnreadable):
        client._load_dialogs(KEY)


def test_a_deleted_dialog_does_not_trip_the_missing_file_guard(tmp_path):
    _save_dialogs(KEY, _dialogs(tag="a"))
    loaded = client._load_dialogs(KEY)
    del loaded["mid"]
    _save_dialogs(KEY, loaded)
    assert sorted(client._load_dialogs(KEY)) == ["Alpha", "zeta"]
    _save_dialogs(KEY, {})                                  # all of them: still a store
    assert client._load_dialogs(KEY) == {}


def test_backup_and_restore_carry_the_folders(tmp_path):
    (tmp_path / "dialogs-kernel.private").write_text("secret\n")
    _save_dialogs(KEY, _dialogs(names=("pub", "secret")))
    arc, _ = datadir.backup(tmp_path / "b.tar.gz")
    import shutil
    shutil.rmtree(_d())
    shutil.rmtree(_d(True))
    datadir.restore(arc)
    assert sorted(client._load_dialogs(KEY)) == ["pub", "secret"]
    assert datadir._has_store(tmp_path)


def test_the_store_command_switches_both_ways_and_refuses_under_a_live_server(tmp_path, monkeypatch, capsys):
    p = _legacy(tmp_path, RAW)
    monkeypatch.setattr(datadir, "_pid_alive", lambda pid: True)
    (tmp_path / ".serve.pid").write_text(str(os.getpid() + 100000))
    assert cli.main(["store", "--to-folder"]) == 1
    assert "stop it first" in capsys.readouterr().err and p.exists()
    monkeypatch.setattr(datadir, "_pid_alive", lambda pid: False)
    assert cli.main(["store", "--to-folder"]) == 0 and _d().is_dir() and not p.exists()
    assert cli.main(["store", "--to-single"]) == 0
    assert json.loads(p.read_text()) == RAW and not _d().exists()
    assert cli.main(["store"]) == 2
