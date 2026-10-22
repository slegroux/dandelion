"""`dandelion serve` refuses to start beside another live server on the same data folder."""
import os
import subprocess
from pathlib import Path

import pytest
import uvicorn

from dandelion import cli, datadir


@pytest.fixture(autouse=True)
def _serve(tmp_path, monkeypatch):
    monkeypatch.setenv("DANDELION_DATA", str(tmp_path))
    monkeypatch.setenv("DANDELION_HOST", "127.0.0.1")
    monkeypatch.delenv("DANDELION_RELOAD", raising=False)
    monkeypatch.setattr(cli, "_move_out_of_repo", lambda: True)
    started = []
    monkeypatch.setattr(uvicorn, "run", lambda *a, **k: started.append(1))
    return started


# subprocess is imported inside datadir._looks_like_dandelion, so these tests patch
# subprocess.run itself (monkeypatch restores it after each test).
def _record(tmp_path, monkeypatch, pid, command, alive=True):
    (tmp_path / ".serve.pid").write_text(str(pid))
    monkeypatch.setattr(datadir, "_pid_alive", lambda p: alive)
    monkeypatch.setattr(subprocess, "run",
                        lambda *a, **k: subprocess.CompletedProcess(a, 0, command + "\n", ""))


def test_live_dandelion_server_refuses(tmp_path, monkeypatch, _serve):
    _record(tmp_path, monkeypatch, os.getpid() + 1, "python -m dandelion.cli serve")
    with pytest.raises(SystemExit) as e:
        cli.cmd_serve(None)
    msg = str(e.value)
    assert str(tmp_path) in msg and str(os.getpid() + 1) in msg
    assert "overwrite each other's dialogs" in msg and "just stop" in msg and "DANDELION_DATA" in msg
    assert not _serve


def test_dead_pid_starts(tmp_path, monkeypatch, _serve):
    _record(tmp_path, monkeypatch, os.getpid() + 1, "python -m dandelion.cli serve", alive=False)
    assert cli.cmd_serve(None) == 0 and _serve


def test_live_pid_of_unrelated_process_starts(tmp_path, monkeypatch, _serve):
    _record(tmp_path, monkeypatch, os.getpid() + 1, "/usr/bin/vim notes.txt")
    assert cli.cmd_serve(None) == 0 and _serve


def test_own_pid_starts(tmp_path, monkeypatch, _serve):
    _record(tmp_path, monkeypatch, os.getpid(), "python -m dandelion.cli serve")
    assert cli.cmd_serve(None) == 0 and _serve


def test_refusal_message_leads_with_kill_and_the_stale_file_hint(tmp_path, monkeypatch):
    _record(tmp_path, monkeypatch, 4242, "python -m dandelion.cli serve")
    with pytest.raises(SystemExit) as e:
        cli.cmd_serve(None)
    msg = str(e.value)
    assert msg.index("kill 4242") < msg.index("just stop")
    assert f"delete {tmp_path / '.serve.pid'}" in msg


def test_refusal_writes_nothing_and_prints_no_banner(tmp_path, monkeypatch, capsys):
    called = []
    monkeypatch.setattr(cli, "_move_out_of_repo", lambda: called.append(1) or True)
    _record(tmp_path, monkeypatch, 4242, "python -m dandelion.cli serve")
    with pytest.raises(SystemExit):
        cli.cmd_serve(None)
    assert not called and "Dandelion UI" not in capsys.readouterr().out
    assert (tmp_path / ".serve.pid").read_text() == "4242"


def test_claim_records_our_pid_over_a_stale_file(tmp_path, monkeypatch):
    _record(tmp_path, monkeypatch, 4242, "python -m dandelion.cli serve", alive=False)
    datadir.claim_serving()
    assert (tmp_path / ".serve.pid").read_text() == str(os.getpid())


def test_claim_creates_the_file_when_none_exists(tmp_path):
    datadir.claim_serving()
    assert (tmp_path / ".serve.pid").read_text() == str(os.getpid())


def test_claim_refuses_when_the_record_cannot_be_written(tmp_path, monkeypatch, _serve):
    def deny(*a, **k):
        raise PermissionError(13, "Permission denied")
    monkeypatch.setattr(os, "open", deny)
    monkeypatch.setattr(os, "link", deny)
    with pytest.raises(SystemExit) as e:
        cli.cmd_serve(None)
    assert str(tmp_path / ".serve.pid") in str(e.value) and "Permission denied" in str(e.value)
    assert not _serve


@pytest.mark.parametrize("command,expected", [
    ("/x/.venv/bin/python3 -m dandelion.cli serve", True),
    ("/x/.venv/bin/python3 -m pappus.cli serve", True),
    ("/x/.venv/bin/python3 -m sidekick.cli serve", True),
    ("/x/.venv/bin/dandelion serve", True),
    ("tail -f /Users/u/Library/Logs/Dandelion/ui.log", False),
    ("/x/.venv/bin/python3 -m dandelion.cli backup", False),
    ("/x/.venv/bin/python3 -m server.kernel_server --port 5055", False),
])
def test_command_line_matcher(monkeypatch, command, expected):
    monkeypatch.setattr(datadir, "_pid_alive", lambda p: True)
    monkeypatch.setattr(subprocess, "run",
                        lambda *a, **k: subprocess.CompletedProcess(a, 0, command + "\n", ""))
    assert datadir._looks_like_dandelion(99) is expected


def _ps_raises(exc):
    def run(*a, **k):
        raise exc
    return run


@pytest.mark.parametrize("run", [
    _ps_raises(FileNotFoundError("ps")),
    _ps_raises(subprocess.TimeoutExpired("ps", 5)),
    lambda *a, **k: subprocess.CompletedProcess(a, 1, "", "boom"),
    lambda *a, **k: subprocess.CompletedProcess(a, 0, "\n", ""),
])
def test_ps_failures_keep_alive_meaning_running(tmp_path, monkeypatch, run):
    (tmp_path / ".serve.pid").write_text("4242")
    monkeypatch.setattr(datadir, "_pid_alive", lambda p: True)
    monkeypatch.setattr(subprocess, "run", run)
    assert datadir.running_server_pid() == 4242


def test_ps_failure_for_a_pid_that_died_means_not_running(tmp_path, monkeypatch):
    (tmp_path / ".serve.pid").write_text("4242")
    alive = iter([True, False])                       # alive at the check, gone by ps
    monkeypatch.setattr(datadir, "_pid_alive", lambda p: next(alive))
    monkeypatch.setattr(subprocess, "run",
                        lambda *a, **k: subprocess.CompletedProcess(a, 1, "", ""))
    assert datadir.running_server_pid() is None


def test_pappus_era_server_blocks_adopt_and_restore(tmp_path, monkeypatch):
    monkeypatch.delenv("DANDELION_DATA", raising=False)
    home = tmp_path / "home"
    old = home / ".config" / "pappus"
    old.mkdir(parents=True)
    (old / "dialogs-kernel.json").write_text("{}")
    (old / ".serve.pid").write_text(str(os.getppid()))
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: subprocess.CompletedProcess(
        a, 0, "/x/.venv/bin/python3 -m pappus.cli serve\n", ""))
    with pytest.raises(datadir.LegacyAppRunning):
        datadir.adopt_legacy_root(home=home)
    monkeypatch.setenv("DANDELION_DATA", str(old))
    arc, _ = datadir.backup(tmp_path / "b.tar.gz")
    with pytest.raises(datadir.AppRunning):
        datadir.restore(arc)


def test_claim_never_leaves_the_file_without_content(tmp_path, monkeypatch):
    real_link, seen = os.link, []

    def link(src, dst):
        seen.append(Path(src).read_text())       # the content is there before f exists
        assert not os.path.exists(dst)
        real_link(src, dst)
    monkeypatch.setattr(os, "link", link)
    datadir.claim_serving()
    assert seen == [str(os.getpid())]
    assert (tmp_path / ".serve.pid").read_text() == str(os.getpid())
    assert not list(tmp_path.glob(".serve.pid.*"))


def test_claim_keeps_a_record_a_racing_claim_just_created(tmp_path, monkeypatch):
    rec = tmp_path / ".serve.pid"
    pids = iter([None, 4242])                    # nobody at the first check, a server at the second

    def racing_link(src, dst):
        rec.write_text("4242")
        raise FileExistsError
    monkeypatch.setattr(os, "link", racing_link)
    monkeypatch.setattr(datadir, "running_server_pid", lambda root=None: next(pids))
    with pytest.raises(datadir.AppRunning):
        datadir.claim_serving()
    assert rec.read_text() == "4242" and not list(tmp_path.glob(".serve.pid.*"))


def test_claim_falls_back_when_links_are_unsupported(tmp_path, monkeypatch):
    def nolink(src, dst):
        raise PermissionError(1, "not supported")
    monkeypatch.setattr(os, "link", nolink)
    datadir.claim_serving()
    assert (tmp_path / ".serve.pid").read_text() == str(os.getpid())
    assert not list(tmp_path.glob(".serve.pid.*"))
