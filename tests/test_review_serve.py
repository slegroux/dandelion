"""`dandelion serve` refuses to start beside another live server on the same data folder."""
import os
import subprocess

import pytest
import uvicorn

from dandelion import cli, datadir


@pytest.fixture(autouse=True)
def _serve(tmp_path, monkeypatch):
    monkeypatch.setenv("DANDELION_DATA", str(tmp_path))
    monkeypatch.setenv("DANDELION_HOST", "127.0.0.1")
    monkeypatch.delenv("DANDELION_RELOAD", raising=False)
    monkeypatch.setattr(cli, "_move_out_of_repo", lambda: True)
    monkeypatch.setattr(datadir, "mark_serving", lambda: None)
    started = []
    monkeypatch.setattr(uvicorn, "run", lambda *a, **k: started.append(1))
    return started


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
