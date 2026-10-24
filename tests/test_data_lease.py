"""Two machines sharing one data folder (pCloud): the one that has it open holds a
lease; another machine that finds it fresh opens the folder read-only."""
import json
import os
import subprocess
import sys
import tarfile
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from starlette.testclient import TestClient

from dandelion import client, datadir


@pytest.fixture
def root(tmp_path, monkeypatch):
    """A marked data folder (conftest), on a machine called mac-b."""
    monkeypatch.setattr(datadir, "_host", lambda: "mac-b")
    return tmp_path


def _stamp(ago_s=0):
    return (datetime.now(timezone.utc) - timedelta(seconds=ago_s)).isoformat(timespec="seconds")


def _lease(root, host="mac-a", pid=4242, ago_s=0, since_ago_s=3600):
    rec = {"host": host, "pid": pid, "since": _stamp(since_ago_s),
           "heartbeat": _stamp(ago_s), "version": "0.1.0"}
    (root / datadir.LEASE).write_text(json.dumps(rec))
    return rec


def _on_disk(root):
    return json.loads((root / datadir.LEASE).read_text())


def _store(root, dialogs):
    (root / "dialogs-kernel.json").write_text(json.dumps(
        {name: [{"id": i, "msg_type": "note", "content": c} for i, c in cells]
         for name, cells in dialogs.items()}))


def _contents(root):
    return {k: [m["content"] for m in v]
            for k, v in json.loads((root / "dialogs-kernel.json").read_text()).items()}


# ---- acquiring ---------------------------------------------------------------
def test_no_other_host_claims_as_before(root):
    datadir.claim_serving()
    assert (root / ".serve.pid").read_text() == str(os.getpid())
    rec = _on_disk(root)
    assert (rec["host"], rec["pid"]) == ("mac-b", os.getpid()) and rec["since"] == rec["heartbeat"]
    assert datadir.store_refusal() is None
    client._save_dialogs("kernel", {"d": []})
    assert (root / "dialogs-kernel.json").is_file()


def test_fresh_lease_from_another_host_opens_read_only(root, capsys):
    _store(root, {"d": [("1", "theirs")]})
    before = (root / "dialogs-kernel.json").read_bytes()
    _lease(root, ago_s=120)
    datadir.claim_serving()
    assert _on_disk(root)["host"] == "mac-a"                     # not taken
    banner = datadir.lease_banner()
    assert banner.startswith("In use by mac-a since ") and "(last seen 2 min ago)" in banner
    assert banner.endswith("Dandelion is read-only here.")
    with pytest.raises(datadir.StoreInUse):
        datadir.check_writable(root / "dialogs-kernel.json")
    client._save_dialogs("kernel", {"d": [client.Msg(id="1", msg_type="note", content="mine")]})
    client._save_dialogs("kernel", {"d": []})
    assert (root / "dialogs-kernel.json").read_bytes() == before
    err = capsys.readouterr().err
    assert err.count("not saving: In use by mac-a") == 1          # logged once


def test_stale_lease_is_taken(root):
    _lease(root, ago_s=datadir.LEASE_STALE_S + 60)
    assert datadir.acquire_lease()
    assert _on_disk(root)["host"] == "mac-b" and datadir.lease_banner() is None


def test_our_hosts_leftover_lease_is_taken(root):
    _lease(root, host="mac-b", pid=1)                            # a crashed run here
    assert datadir.acquire_lease()
    assert _on_disk(root)["pid"] == os.getpid()


def test_refused_root_gets_no_lease(tmp_path, monkeypatch):
    bare = tmp_path / "unmounted"
    bare.mkdir()
    monkeypatch.setenv("DANDELION_DATA", str(bare))
    datadir.claim_serving()
    assert not datadir.acquire_lease()
    assert list(bare.iterdir()) == []


def test_the_lease_is_not_carried_in_a_backup(root):
    (root / "recall.json").write_text("{}")
    datadir.acquire_lease()
    arc, _ = datadir.backup(root / "b.tar.gz")
    with tarfile.open(arc) as t:
        names = t.getnames()
    assert any(n.endswith("recall.json") for n in names)
    assert not any(datadir.LEASE in n for n in names)


def test_restore_refuses_while_another_host_has_the_folder_open(root):
    _lease(root)
    with pytest.raises(datadir.AppRunning, match="open on mac-a"):
        datadir.restore(root / "nothing.tar.gz")


# ---- while running -------------------------------------------------------------
def test_heartbeat_rewrites_the_lease_atomically(root, monkeypatch):
    datadir.acquire_lease()
    rec = _on_disk(root)
    (root / datadir.LEASE).write_text(json.dumps({**rec, "heartbeat": "2000-01-01T00:00:00+00:00"}))
    replaced = []
    real = os.replace
    monkeypatch.setattr(datadir.os, "replace", lambda a, b: (replaced.append((Path(a), Path(b))), real(a, b)))
    assert datadir._tick()
    after = _on_disk(root)
    assert after["heartbeat"] != "2000-01-01T00:00:00+00:00"
    assert {k: after[k] for k in ("host", "pid", "since")} == {k: rec[k] for k in ("host", "pid", "since")}
    (tmp, dst), = replaced
    assert dst == root / datadir.LEASE and tmp.name.endswith(".tmp") and not tmp.exists()


def test_the_heartbeat_thread_beats(root, monkeypatch):
    monkeypatch.setattr(datadir, "LEASE_HEARTBEAT_S", 0.05)
    datadir.acquire_lease()
    old = {**_on_disk(root), "heartbeat": "2000-01-01T00:00:00+00:00"}
    (root / datadir.LEASE).write_text(json.dumps(old))
    deadline = time.time() + 5
    while _on_disk(root)["heartbeat"] == old["heartbeat"] and time.time() < deadline:
        time.sleep(0.02)
    assert _on_disk(root)["heartbeat"] != old["heartbeat"]


def test_another_host_taking_over_makes_us_read_only(root):
    datadir.acquire_lease()
    theirs = _lease(root, host="mac-a", since_ago_s=0)             # they pressed Take over
    assert not datadir._tick()
    assert datadir.lease_banner().startswith("In use by mac-a")
    assert not datadir._tick()                                   # no more beats
    assert _on_disk(root) == theirs                              # ours never written back
    client._save_dialogs("kernel", {"d": []})
    assert not (root / "dialogs-kernel.json").exists()


def test_this_hosts_reload_worker_taking_the_lease_stops_our_beat_quietly(root):
    datadir.acquire_lease()
    _lease(root, host="mac-b", pid=os.getpid() + 1)
    assert not datadir._tick()
    assert datadir.store_refusal() is None


def test_clean_shutdown_removes_only_our_own_lease(root):
    datadir.acquire_lease()
    datadir._release_lease()
    assert not (root / datadir.LEASE).exists()
    datadir.acquire_lease()
    theirs = _lease(root, host="mac-a")
    datadir._release_lease()
    assert _on_disk(root) == theirs


# ---- leaving read-only -----------------------------------------------------------
@pytest.fixture
def ui(root, monkeypatch):
    from dandelion import app
    _store(root, {"d": [("1", "old")]})
    _lease(root, ago_s=30)
    datadir.acquire_lease()
    backend = client._InMemoryBackend("kernel")                   # loaded while read-only
    monkeypatch.setitem(app.STATE, "backend", backend)
    return app, backend


def test_page_shows_the_banner_with_both_actions(ui):
    app, _ = ui
    page = TestClient(app.app).get("/").text
    assert "In use by mac-a since" in page and "Dandelion is read-only here." in page
    assert 'action="/lease/check"' in page and 'action="/lease/take-over"' in page
    assert ("Take over only if mac-a is not running Dandelion on this folder; "
            "unsynced edits there may be lost.") in page
    assert "In use by mac-a" in TestClient(app.app).get("/settings").text


def test_check_again_stays_read_only_while_the_lease_is_fresh(ui, root):
    app, _ = ui
    TestClient(app.app).post("/lease/check")
    assert datadir.lease_banner() and _on_disk(root)["host"] == "mac-a"


def test_take_over_needs_the_confirmation(ui, root):
    app, _ = ui
    TestClient(app.app).post("/lease/take-over")
    assert datadir.lease_banner() and _on_disk(root)["host"] == "mac-a"
    TestClient(app.app).post("/lease/take-over", data={"confirm": "on"})
    assert datadir.lease_banner() is None and _on_disk(root)["host"] == "mac-b"


def test_leaving_read_only_reloads_the_store_from_disk(ui, root):
    app, backend = ui
    backend.add("d", "typed while read-only", "note")             # refused, kept in memory only
    _store(root, {"d": [("1", "old"), ("2", "saved on mac-a")]})  # mac-a kept working
    _lease(root, ago_s=datadir.LEASE_STALE_S + 1)                # then stopped
    TestClient(app.app).post("/lease/check")
    assert datadir.lease_banner() is None and _on_disk(root)["host"] == "mac-b"
    assert [m.content for m in backend.messages("d")] == ["old", "saved on mac-a"]
    backend.add("d", "new here", "note")
    assert _contents(root)["d"] == ["old", "saved on mac-a", "new here"]


# ---- two real processes --------------------------------------------------------
_PROC = r"""
import json, sys
from dandelion import client, datadir
datadir._host = lambda: sys.argv[1]
datadir.freeze_root()
datadir.claim_serving()
client._save_dialogs("kernel", {"d": [client.Msg(id="1", msg_type="note", content=sys.argv[1])]})
print(json.dumps({"banner": datadir.lease_banner()}), flush=True)
sys.stdin.read()                       # hold the folder open until told to stop
"""


def test_two_processes_with_different_hosts(tmp_path):
    env = {**os.environ, "DANDELION_DATA": str(tmp_path), "HOME": str(tmp_path / "home"),
           "PYTHONPATH": str(Path(__file__).resolve().parent.parent)}
    procs = []
    try:
        out = []
        for host in ("mac-a", "mac-b"):
            p = subprocess.Popen([sys.executable, "-c", _PROC, host], env=env, text=True,
                                 stdin=subprocess.PIPE, stdout=subprocess.PIPE)
            procs.append(p)
            out.append(json.loads(p.stdout.readline()))
        assert out[0]["banner"] is None
        assert out[1]["banner"].startswith("In use by mac-a since ")
        assert _contents(tmp_path) == {"d": ["mac-a"]}             # mac-b saved nothing
        assert _on_disk(tmp_path)["host"] == "mac-a"
        procs[1].communicate("", timeout=30)
        assert _on_disk(tmp_path)["host"] == "mac-a"               # b's exit left a's lease
        procs[0].communicate("", timeout=30)
        assert not (tmp_path / datadir.LEASE).exists()             # a's clean exit removed it
    finally:
        for p in procs:
            if p.poll() is None:
                p.kill()
