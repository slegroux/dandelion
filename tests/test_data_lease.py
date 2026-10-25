"""Two machines sharing one data folder (pCloud): the one that has it open holds a
lease; another machine that finds it alive opens the folder read-only. Where a
write must be refused, the tests check the store's bytes are unchanged."""
import json
import os
import queue
import subprocess
import sys
import tarfile
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from starlette.testclient import TestClient

from dandelion import client, datadir

ME, OTHER = "UUID-B", "UUID-A"


@pytest.fixture
def root(tmp_path, monkeypatch):
    """A marked data folder (conftest), on the Mac 'Mac B' (hostname MacBook-Pro)."""
    monkeypatch.setattr(datadir, "_machine", lambda: ME)
    monkeypatch.setattr(datadir, "_machine_name", lambda: "Mac B")
    monkeypatch.setattr(datadir, "_host", lambda: "MacBook-Pro")
    return tmp_path


@pytest.fixture
def clock(monkeypatch):
    """datadir's clock, moved forward by `clock.advance(s)` (wall and monotonic)."""
    real = time

    class Clock:
        off = 0.0

        def advance(self, s):
            self.off += s

        def time(self):
            return real.time() + self.off

        def monotonic(self):
            return real.monotonic() + self.off

        def __getattr__(self, name):
            return getattr(real, name)

    c = Clock()
    monkeypatch.setattr(datadir, "time", c)
    return c


def _stamp(ago_s=0):
    return (datetime.now(timezone.utc) - timedelta(seconds=ago_s)).isoformat(timespec="seconds")


def _lease(root, machine=OTHER, name="Mac A", pid=4242, ago_s=0, since_ago_s=3600):
    rec = {"machine": machine, "name": name, "host": "MacBook-Pro", "pid": pid,
           "since": _stamp(since_ago_s), "heartbeat": _stamp(ago_s), "version": "0.1.0"}
    (root / datadir.LEASE).write_text(json.dumps(rec))
    return rec


def _on_disk(root):
    return json.loads((root / datadir.LEASE).read_text())


def _store(root, dialogs):
    (root / "dialogs-kernel.json").write_text(json.dumps(
        {name: [{"id": i, "msg_type": "note", "content": c} for i, c in cells]
         for name, cells in dialogs.items()}))
    return (root / "dialogs-kernel.json").read_bytes()


def _store_dir(root, dialogs):
    """What another Mac's save leaves: one file per dialog (the store already split)."""
    d = root / "dialogs-kernel.d"
    d.mkdir(exist_ok=True)
    for name, cells in dialogs.items():
        raw = [{"id": i, "msg_type": "note", "content": c} for i, c in cells]
        client._dialog_file(d, name).write_text(client._dialog_body(name, raw))
    (d / client._INDEX).write_text(json.dumps({"order": list(dialogs)}))


def _contents(root):
    return {k: [m["content"] for m in v] for k, v in client._read_layout("kernel").items()}


def _save(text="mine"):
    client._save_dialogs("kernel", {"d": [client.Msg(id="9", msg_type="note", content=text)]})


# ---- starting --------------------------------------------------------------------
def test_no_other_machine_claims_as_before(root):
    datadir.claim_serving()
    assert (root / ".serve.pid").read_text() == f"{os.getpid()} {ME}"
    rec = _on_disk(root)
    assert (rec["machine"], rec["name"], rec["pid"]) == (ME, "Mac B", os.getpid())
    assert datadir.store_refusal() is None and datadir.lease_notice() is None
    _save()
    assert _contents(root) == {"d": ["mine"]}


def test_a_live_lease_from_another_machine_opens_read_only(root, capsys):
    before = _store(root, {"d": [("1", "theirs")]})
    _lease(root, ago_s=120)
    datadir.claim_serving()
    assert _on_disk(root)["machine"] == OTHER                     # not taken
    banner = datadir.lease_banner()
    assert banner.startswith("In use by Mac A since ") and "(last seen 2 min ago)" in banner
    assert banner.endswith("Dandelion is read-only here.")
    with pytest.raises(datadir.StoreInUse):
        datadir.check_writable(root / "dialogs-kernel.json")
    _save()
    _save("again")
    assert (root / "dialogs-kernel.json").read_bytes() == before
    assert capsys.readouterr().err.count("not saving: In use by Mac A") == 1   # logged once


def test_same_hostname_on_another_machine_is_another_machine(root):
    before = _store(root, {"d": [("1", "theirs")]})
    _lease(root, machine=OTHER, name="MacBook-Pro")              # same hostname, other Mac
    assert not datadir.acquire_lease()
    _save()
    assert (root / "dialogs-kernel.json").read_bytes() == before


def test_a_lease_stamped_in_the_future_counts_as_live(root):
    _lease(root, ago_s=-3600)                                    # its clock is an hour ahead
    assert not datadir.acquire_lease()
    assert "(dated in the future: its clock is ahead)" in datadir.lease_banner()


def test_a_lease_that_stopped_changing_is_taken_after_watching_it(root, clock):
    _lease(root, ago_s=600)                                      # 10 min old by its stamp
    assert not datadir.acquire_lease()                           # at start: may be clock skew
    ok, msg = datadir.take_lease()
    assert not ok and "Still in use by Mac A" in msg
    clock.advance(datadir.LEASE_STALE_S)                         # unchanged that long, by our clock
    ok, msg = datadir.take_lease()
    assert ok and msg.startswith("Took over the data folder from Mac A (last seen ")
    assert _on_disk(root)["machine"] == ME


def test_a_lease_that_keeps_beating_is_never_taken(root, clock):
    _lease(root, ago_s=600)
    datadir.acquire_lease()
    clock.advance(datadir.LEASE_STALE_S - 1)
    _lease(root, ago_s=0)                                        # it beat
    assert not datadir.take_lease()[0]
    clock.advance(datadir.LEASE_STALE_S - 1)
    assert not datadir.take_lease()[0] and _on_disk(root)["machine"] == OTHER


def test_an_old_lease_is_taken_at_start_with_a_notice(root):
    _lease(root, ago_s=2 * 3600)
    assert datadir.acquire_lease()
    assert datadir.lease_notice() == "Took over the data folder from Mac A (last seen 120 min ago)."
    assert _on_disk(root)["machine"] == ME


def test_this_machines_lease_from_a_run_that_ended_is_taken(root):
    _lease(root, machine=ME, pid=999_999)                        # no such process
    assert datadir.acquire_lease() and _on_disk(root)["pid"] == os.getpid()
    assert datadir.lease_notice() is None


def test_another_live_process_on_this_machine_fails_closed(root):
    before = _store(root, {"d": [("1", "x")]})
    _lease(root, machine=ME, pid=os.getppid())                   # alive, not us
    assert not datadir.acquire_lease()
    _save()
    assert (root / "dialogs-kernel.json").read_bytes() == before


def test_reload_worker_takes_its_parents_lease(root, monkeypatch):
    _lease(root, machine=ME, pid=os.getppid())
    monkeypatch.setenv("_DANDELION_RELOAD_WORKER", str(os.getppid()))
    assert datadir.acquire_lease() and _on_disk(root)["pid"] == os.getpid()


def test_an_unreadable_lease_at_start_means_an_unknown_holder(root):
    before = _store(root, {"d": [("1", "x")]})
    (root / datadir.LEASE).write_text('{"machine": "UUID-A", "heartb')   # torn, and stays so
    assert not datadir.acquire_lease()
    assert datadir.lease_banner().startswith("In use by an unknown machine")
    _save()
    assert (root / "dialogs-kernel.json").read_bytes() == before
    assert datadir.take_lease(force=True)[0] and _on_disk(root)["machine"] == ME


def test_refused_root_gets_no_lease(tmp_path, monkeypatch):
    bare = tmp_path / "unmounted"
    bare.mkdir()
    monkeypatch.setenv("DANDELION_DATA", str(bare))
    datadir.claim_serving()
    assert not datadir.acquire_lease()
    assert list(bare.iterdir()) == []


def test_another_machines_pid_record_is_not_a_server_here(root, monkeypatch):
    (root / ".serve.pid").write_text(f"{os.getppid()} {OTHER}")  # a live pid, but over there
    monkeypatch.setattr(datadir, "_looks_like_dandelion", lambda pid: True)
    assert datadir.running_server_pid() is None
    (root / ".serve.pid").write_text(f"{os.getppid()} {ME}")
    assert datadir.running_server_pid() == os.getppid()


def test_machine_local_files_stay_out_of_backup_and_restore(root, monkeypatch):
    cfg = datadir.config_dir()
    monkeypatch.setenv("DANDELION_DATA", str(cfg))                 # the default root
    first = datadir._local_machine_id()
    assert datadir._local_machine_id() == first and (cfg / "machine-id").is_file()
    (cfg / "unsaved-20261023-120000.json").write_text("{}")
    (cfg / "recall.json").write_text("{}")
    datadir.acquire_lease()
    arc, _ = datadir.backup(root / "b.tar.gz")
    with tarfile.open(arc) as t:
        names = t.getnames()
    assert any(n.endswith("recall.json") for n in names)
    assert not any(datadir.LEASE in n or "machine-id" in n or "unsaved-" in n for n in names)
    datadir._release_lease()
    bad = root / "bad.tar.gz"
    with tarfile.open(bad, "w:gz") as t:
        t.add(cfg / "machine-id", arcname="data/machine-id")
    other = root / "other"
    other.mkdir()
    (other / datadir.MARKER).write_text("")
    monkeypatch.setenv("DANDELION_DATA", str(other))
    restored, _ = datadir.restore(bad)
    assert not restored and not (other / "machine-id").exists()


def test_restore_refuses_while_another_machine_has_the_folder_open(root):
    _lease(root)
    with pytest.raises(datadir.AppRunning, match="open on Mac A"):
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
    assert {k: after[k] for k in ("machine", "pid", "since")} == {k: rec[k] for k in ("machine", "pid", "since")}
    (tmp, dst), = replaced
    assert dst == root / datadir.LEASE and tmp.name.endswith(".tmp") and not tmp.exists()


def test_the_heartbeat_thread_beats_and_survives_an_error(root, monkeypatch, capsys):
    monkeypatch.setattr(datadir, "LEASE_HEARTBEAT_S", 0.05)
    real, calls = datadir._tick, []

    def flaky(stop=None):
        calls.append(1)
        if len(calls) == 1:
            raise RuntimeError("boom")
        return real(stop)
    monkeypatch.setattr(datadir, "_tick", flaky)
    datadir.acquire_lease()
    old = {**_on_disk(root), "heartbeat": "2000-01-01T00:00:00+00:00"}
    (root / datadir.LEASE).write_text(json.dumps(old))
    deadline = time.time() + 5
    while _on_disk(root)["heartbeat"] == old["heartbeat"] and time.time() < deadline:
        time.sleep(0.02)
    assert _on_disk(root)["heartbeat"] != old["heartbeat"] and len(calls) >= 2
    assert "lease heartbeat failed: RuntimeError('boom')" in capsys.readouterr().err


def test_an_unreadable_lease_skips_the_beat(root):
    datadir.acquire_lease()
    (root / datadir.LEASE).write_text("{torn")
    assert datadir._tick()                                       # keep beating, write nothing
    assert (root / datadir.LEASE).read_text() == "{torn"


def test_another_machine_taking_over_makes_us_read_only(root):
    datadir.acquire_lease()
    theirs = _lease(root, since_ago_s=0)                         # they pressed Take over
    assert not datadir._tick()
    assert datadir.lease_banner().startswith("In use by Mac A")
    assert not datadir._tick()                                   # no more beats
    assert _on_disk(root) == theirs                              # ours never written back
    _save()
    assert not (root / "dialogs-kernel.json").exists()


def test_same_hostname_other_machine_taking_over_at_a_tick(root):
    datadir.acquire_lease()
    _lease(root, machine=OTHER, name="MacBook-Pro", since_ago_s=0)
    assert not datadir._tick() and datadir.store_refusal()


def test_only_the_reload_parent_steps_back_quietly(root, monkeypatch):
    datadir.acquire_lease()
    _lease(root, machine=ME, pid=os.getpid() + 1, since_ago_s=0)  # another process here
    assert not datadir._tick()
    assert "read-only" in datadir.store_refusal()                # not the reload parent: fail closed
    datadir._reset_lease()
    monkeypatch.setenv("_DANDELION_RELOAD_WORKER", str(os.getpid()))   # we ran serve with reload
    (root / datadir.LEASE).unlink()
    datadir.acquire_lease()
    _lease(root, machine=ME, pid=os.getpid() + 1, since_ago_s=0)  # our worker took it
    assert not datadir._tick() and datadir.store_refusal() is None


def test_a_hold_that_lapsed_refuses_saves_and_never_beats(root, clock):
    before = _store(root, {"d": [("1", "B: an hour of work")]})
    datadir.acquire_lease()
    lease = (root / datadir.LEASE).read_bytes()
    clock.advance(datadir.LEASE_STALE_S)                         # asleep, by our own clock
    _save("A: typed on wake")
    assert (root / "dialogs-kernel.json").read_bytes() == before
    with pytest.raises(datadir.StoreInUse, match="hold on the data folder lapsed"):
        datadir.check_writable()
    assert not datadir._tick() and (root / datadir.LEASE).read_bytes() == lease
    ok, msg = datadir.take_lease()
    assert not ok and msg.startswith("Waiting for the other Mac's changes to sync")
    clock.advance(datadir.LEASE_GRACE_S)
    assert datadir.take_lease()[0]                               # nobody took it: ours again
    _save("after")
    assert _contents(root) == {"d": ["after"]}


def test_a_lapse_shows_who_took_over(root, clock):
    datadir.acquire_lease()
    clock.advance(datadir.LEASE_STALE_S)
    assert "lapsed" in datadir.store_refusal()
    _lease(root, since_ago_s=0)                                  # Mac A took it while we slept
    clock.advance(datadir.LEASE_GRACE_S)
    ok, msg = datadir.take_lease()
    assert not ok and msg.startswith("Still in use by Mac A.")
    assert datadir.lease_banner().startswith("In use by Mac A")


def test_clean_shutdown_removes_only_our_own_lease(root):
    datadir.acquire_lease()
    datadir._release_lease()
    assert not (root / datadir.LEASE).exists()
    datadir.acquire_lease()
    theirs = _lease(root)
    datadir._release_lease()
    assert _on_disk(root) == theirs


def test_a_corrupt_store_is_not_repaired_while_read_only(root):
    (root / "dialogs-kernel.json").write_text("{torn")
    _lease(root)
    datadir.acquire_lease()
    with pytest.raises(client.StoreUnreadable):
        client._load_dialogs("kernel")
    assert (root / "dialogs-kernel.json").read_text() == "{torn"
    assert not list(root.glob("*.corrupt-*"))


# ---- the page and leaving read-only ----------------------------------------------
@pytest.fixture
def ui(root, monkeypatch):
    from dandelion import app
    _store(root, {"d": [("1", "old")]})
    _lease(root, ago_s=30)
    datadir.acquire_lease()
    backend = client._InMemoryBackend("kernel")                   # loaded while read-only
    monkeypatch.setitem(app.STATE, "backend", backend)
    monkeypatch.setitem(app.STATE, "pending_stream", None)
    return app, backend


def test_page_shows_the_banner_with_both_actions(ui):
    app, _ = ui
    page = TestClient(app.app).get("/").text
    assert "In use by Mac A since" in page and "Dandelion is read-only here." in page
    assert 'action="/lease/check"' in page and 'action="/lease/take-over"' in page
    assert ("Take over only if Mac A is not running Dandelion on this folder; "
            "unsynced edits there may be lost.") in page
    assert 'hx-get="/lease/status"' in page and 'hx-trigger="every 30s"' in page
    assert "In use by Mac A" in TestClient(app.app).get("/settings").text


def test_htmx_partials_carry_the_banner_out_of_band(ui):
    app, _ = ui
    tc = TestClient(app.app)
    r = tc.get("/stream/refresh", headers={"HX-Request": "true"})
    assert 'id="ro-banner"' in r.text and 'hx-swap-oob="true"' in r.text and "In use by Mac A" in r.text
    assert int(r.headers["content-length"]) == len(r.content)
    s = tc.get("/lease/status", headers={"HX-Request": "true"}).text
    assert "In use by Mac A" in s and "hx-swap-oob" not in s


def test_partials_carry_no_banner_while_writable(root):
    from dandelion import app
    datadir.acquire_lease()
    r = TestClient(app.app).get("/stream/refresh", headers={"HX-Request": "true"})
    assert "ro-banner" not in r.text


def test_check_again_stays_read_only_while_the_lease_is_alive(ui, root):
    app, _ = ui
    r = TestClient(app.app).post("/lease/check", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/"
    assert datadir.lease_banner() and _on_disk(root)["machine"] == OTHER


def test_take_over_needs_the_confirmation(ui, root):
    app, _ = ui
    TestClient(app.app).post("/lease/take-over")
    assert datadir.lease_banner() and _on_disk(root)["machine"] == OTHER
    TestClient(app.app).post("/lease/take-over", data={"confirm": "on"})
    assert datadir.lease_banner() is None and _on_disk(root)["machine"] == ME


def test_leaving_read_only_reloads_and_keeps_what_was_in_memory(ui, root):
    app, backend = ui
    backend.add("d", "typed while read-only", "note")             # refused, kept in memory only
    _store_dir(root, {"d": [("1", "old"), ("2", "saved on Mac A")]})  # Mac A kept working
    page = TestClient(app.app).post("/lease/take-over", data={"confirm": "on"}).text
    assert datadir.lease_banner() is None and _on_disk(root)["machine"] == ME
    assert [m.content for m in backend.messages("d")] == ["old", "saved on Mac A"]
    kept, = datadir.config_dir().glob("unsaved-*.json")
    assert [m["content"] for m in json.loads(kept.read_text())["d"]] == ["old", "typed while read-only"]
    assert str(kept) in page
    backend.add("d", "new here", "note")
    assert _contents(root)["d"] == ["old", "saved on Mac A", "new here"]


def test_no_leaving_read_only_while_an_answer_streams(ui, root):
    app, _ = ui
    app.STATE["pending_stream"] = ("d", "1")
    page = TestClient(app.app).post("/lease/take-over", data={"confirm": "on"}).text
    assert "still coming in" in page and _on_disk(root)["machine"] == OTHER


def test_a_missing_store_at_reload_keeps_the_lease_and_the_dialogs(ui, root):
    app, backend = ui
    bak = root / "dialogs-kernel.json.bak"
    bak.write_text((root / "dialogs-kernel.json").read_text())
    (root / "dialogs-kernel.json").unlink()                      # mid-replace on the mount
    page = TestClient(app.app).post("/lease/take-over", data={"confirm": "on"}).text
    assert "Still read-only" in page and _on_disk(root)["machine"] == OTHER
    assert not (root / "dialogs-kernel.json").exists()
    assert [m.content for m in backend.messages("d")] == ["old"]
    bak.unlink()                                                 # no backups either: still not {}
    TestClient(app.app).post("/lease/take-over", data={"confirm": "on"})
    assert _on_disk(root)["machine"] == OTHER and [m.content for m in backend.messages("d")] == ["old"]


# ---- after a re-review: sleep on one Mac, stalled writes, local files ------------
def test_the_default_root_never_lapses(root, clock, monkeypatch):
    cfg = datadir.config_dir()
    cfg.mkdir(parents=True)
    monkeypatch.setenv("DANDELION_DATA", str(cfg))
    datadir.acquire_lease()
    clock.advance(10 * 60)                                       # a night's sleep, one Mac
    assert datadir.store_refusal() is None and datadir.lease_banner() is None
    client._save_dialogs("kernel", {"d": [client.Msg(id="1", msg_type="note", content="on wake")]})
    assert client._read_layout("kernel")["d"][0]["content"] == "on wake"
    assert datadir._tick() and "another machine" not in datadir.lease_status()


@pytest.fixture
def awake(root, clock, monkeypatch):
    """Mac B held a shared folder, slept past its lease, and typed on wake (refused)."""
    from dandelion import app
    _store(root, {"d": [("1", "old")]})
    datadir.acquire_lease()
    backend = client._InMemoryBackend("kernel")
    monkeypatch.setitem(app.STATE, "backend", backend)
    monkeypatch.setitem(app.STATE, "pending_stream", None)
    clock.advance(datadir.LEASE_STALE_S)
    backend.add("d", "typed on wake", "note")
    assert _contents(root) == {"d": ["old"]}                     # refused: the hold lapsed
    clock.advance(datadir.LEASE_GRACE_S)
    return app, backend


def test_waking_with_an_unchanged_store_keeps_and_saves_the_edits(awake, root):
    app, backend = awake
    page = TestClient(app.app).post("/lease/check").text
    assert "your edits were kept and saved" in page
    assert _contents(root) == {"d": ["old", "typed on wake"]}
    assert not list(datadir.local_dir().glob("unsaved-*"))


def test_waking_to_a_store_the_other_mac_changed_reloads_and_keeps_the_edits_aside(awake, root):
    app, backend = awake
    _store_dir(root, {"d": [("1", "old"), ("2", "saved on Mac A")]})  # its save synced, its lease not
    page = TestClient(app.app).post("/lease/check").text
    assert "Dialogs reloaded from disk." in page
    assert [m.content for m in backend.messages("d")] == ["old", "saved on Mac A"]
    kept, = datadir.local_dir().glob("unsaved-*.json")
    assert [m["content"] for m in json.loads(kept.read_text())["d"]] == ["old", "typed on wake"]
    assert _contents(root) == {"d": ["old", "saved on Mac A"]}


def test_a_heartbeat_write_that_stalls_past_the_fence_lapses(root, clock, monkeypatch):
    before = _store(root, {"d": [("1", "B: work saved while A's write hung")]})
    datadir.acquire_lease()
    real = datadir._write_lease

    def stalled(rec):
        clock.advance(datadir.LEASE_STALE_S)                     # hung in the mount meanwhile
        real(rec)
    monkeypatch.setattr(datadir, "_write_lease", stalled)
    assert not datadir._tick()
    assert "lapsed" in datadir.store_refusal()
    _save("A edit")
    assert (root / "dialogs-kernel.json").read_bytes() == before
    monkeypatch.setattr(datadir, "_write_lease", real)
    assert not datadir._tick()                                   # never beats again


def test_a_stalled_write_overtaken_by_a_take_over_shows_who(root, clock, monkeypatch):
    datadir.acquire_lease()
    real = datadir._write_lease

    def stalled(rec):
        real(rec)
        clock.advance(datadir.LEASE_STALE_S)
        _lease(root, since_ago_s=0)                              # Mac A's take-over lands after ours
    monkeypatch.setattr(datadir, "_write_lease", stalled)
    assert not datadir._tick()
    assert datadir.lease_banner().startswith("In use by Mac A")
    assert _on_disk(root)["machine"] == OTHER


def test_the_banner_check_runs_off_the_event_loop(root, clock):
    import asyncio
    from dandelion import app
    datadir.acquire_lease()
    clock.advance(datadir.LEASE_STALE_S)                         # the check now needs _LEASE_LOCK
    held, release = threading.Event(), threading.Event()

    def hold():
        with datadir._LEASE_LOCK:
            held.set()
            release.wait(10)
    threading.Thread(target=hold, daemon=True).start()
    held.wait(5)

    async def inner(scope, receive, send):
        await send({"type": "http.response.start", "status": 200,
                    "headers": [(b"content-type", b"text/html; charset=utf-8")]})
        await send({"type": "http.response.body", "body": b"<div>x</div>"})
    sent = []

    async def send(m):
        sent.append(m)

    async def main():
        task = asyncio.create_task(app._ReadOnlyBanner(inner)(
            {"type": "http", "path": "/x", "headers": [(b"hx-request", b"true")]}, None, send))
        t0 = time.monotonic()
        await asyncio.sleep(0.2)                                 # the loop keeps running
        assert time.monotonic() - t0 < 2 and not task.done()
        release.set()
        await asyncio.wait_for(task, 10)
    asyncio.run(main())
    assert b"lapsed" in sent[-1]["body"]


def test_machine_local_files_never_go_to_a_linked_config_dir(root, tmp_path):
    shared = tmp_path / "pcloud" / "dandelion"
    shared.mkdir(parents=True)
    cfg = datadir.config_dir()
    cfg.parent.mkdir(parents=True, exist_ok=True)
    cfg.symlink_to(shared)                                       # an old ~/.config/pappus link
    local = datadir.local_dir()
    assert not local.resolve().is_relative_to(shared.resolve())
    datadir._local_machine_id()
    assert (local / "machine-id").is_file()
    _store(root, {"d": [("1", "old")]})
    backend = client._InMemoryBackend("kernel")
    _lease(root)
    datadir.acquire_lease()                                      # read-only
    backend.add("d", "typed", "note")
    kept = backend.reload()
    assert kept.parent == local and list(shared.iterdir()) == []


def test_unsaved_files_only_for_refused_edits_and_never_overwritten(ui, root):
    app, backend = ui
    assert backend.reload() is None                              # nothing typed: nothing kept
    backend.add("d", "one", "note")
    first = backend.reload()
    backend.add("d", "two", "note")
    second = backend.reload()
    assert first != second and first.is_file() and second.is_file()


def test_a_refused_reload_leaves_no_take_over_notice(root, clock):
    _lease(root, ago_s=0)
    assert not datadir.acquire_lease()
    clock.advance(datadir.LEASE_STALE_S)                         # watched unchanged: may be taken

    def refuse():
        raise client.StoreUnreadable("store missing")
    with pytest.raises(client.StoreUnreadable):
        datadir.take_lease(before=refuse)
    assert datadir.lease_notice() is None and datadir.lease_banner().startswith("In use by Mac A")


def test_still_in_use_says_when_check_again_can_work(root):
    _lease(root)
    datadir.acquire_lease()
    ok, msg = datadir.take_lease()
    assert not ok and "If Mac A has quit or crashed, Check again works once its lease has " \
        "stayed unchanged for 5 min (from " in msg and msg.endswith("or use Take over.")


def test_the_heartbeat_is_kept_under_half_the_staleness(capsys):
    assert datadir._heartbeat_for(60, 300) == 60
    assert datadir._heartbeat_for(300, 300) == 75
    assert "must be under half" in capsys.readouterr().err


def test_the_status_poll_stays_out_of_the_access_log():
    import logging
    from dandelion import app

    def rec(path):
        return logging.LogRecord("uvicorn.access", logging.INFO, "", 0, '%s - "%s %s HTTP/%s" %d',
                                 ("127.0.0.1:5", "GET", path, "1.1", 200), None)
    assert not app._QuietLeasePoll().filter(rec("/lease/status"))
    assert app._QuietLeasePoll().filter(rec("/"))


# ---- two real processes --------------------------------------------------------
_PROC = r"""
import json, sys
from dandelion import client, datadir
datadir._machine = lambda: sys.argv[1]
datadir._machine_name = lambda: sys.argv[1]
datadir.freeze_root()
datadir.claim_serving()
client._save_dialogs("kernel", {"d": [client.Msg(id="1", msg_type="note", content=sys.argv[1])]})
print(json.dumps({"banner": datadir.lease_banner()}), flush=True)
sys.stdin.read()                       # hold the folder open until told to stop
"""


def _first_line(p, timeout=60):
    q = queue.Queue()
    threading.Thread(target=lambda: q.put(p.stdout.readline()), daemon=True).start()
    return json.loads(q.get(timeout=timeout))


def test_two_processes_on_different_machines(tmp_path):
    env = {**os.environ, "DANDELION_DATA": str(tmp_path), "HOME": str(tmp_path / "home"),
           "PYTHONPATH": str(Path(__file__).resolve().parent.parent)}
    procs = []
    try:
        out = []
        for machine in ("Mac-A", "Mac-B"):
            p = subprocess.Popen([sys.executable, "-c", _PROC, machine], env=env, text=True,
                                 stdin=subprocess.PIPE, stdout=subprocess.PIPE)
            procs.append(p)
            out.append(_first_line(p))
        assert out[0]["banner"] is None
        assert out[1]["banner"].startswith("In use by Mac-A since ")
        assert _contents(tmp_path) == {"d": ["Mac-A"]}             # Mac-B saved nothing
        assert _on_disk(tmp_path)["machine"] == "Mac-A"
        procs[1].communicate("", timeout=30)
        assert _on_disk(tmp_path)["machine"] == "Mac-A"            # B's exit left A's lease
        procs[0].communicate("", timeout=30)
        assert not (tmp_path / datadir.LEASE).exists()             # A's clean exit removed it
    finally:
        for p in procs:
            if p.poll() is None:
                p.kill()


# ---- a missing store beside its backups is never loaded as empty -----------------
def test_a_missing_store_with_a_bak_is_refused_on_a_non_default_root(root):
    (root / "dialogs-kernel.json.bak").write_text('{"d": []}')
    with pytest.raises(client.StoreUnreadable):
        client._load_dialogs("kernel")


def test_a_missing_store_with_a_bak_is_refused_on_a_symlinked_default_root(tmp_path, monkeypatch):
    real = tmp_path / "mount"
    real.mkdir()
    cfg = datadir.config_dir()
    cfg.parent.mkdir(parents=True)
    cfg.symlink_to(real)                                         # the user's link into pCloud
    monkeypatch.setenv("DANDELION_DATA", str(cfg))
    (real / "dialogs-kernel.json.bak").write_text('{"d": []}')
    with pytest.raises(client.StoreUnreadable):
        client._wait_for_store(cfg / "dialogs-kernel.json")


def test_a_missing_store_on_the_real_default_root_starts_empty(tmp_path, monkeypatch):
    cfg = datadir.config_dir()
    cfg.mkdir(parents=True)
    monkeypatch.setenv("DANDELION_DATA", str(cfg))
    (cfg / "dialogs-kernel.json.bak").write_text('{"d": []}')
    client._wait_for_store(cfg / "dialogs-kernel.json")          # no raise
