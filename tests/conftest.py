import os
import sys
from pathlib import Path

import pytest

# Make `import dandelion` / `import server` work for any test file run alone
# (uv runs the project unpackaged, `package = false`).
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
# A missing store or lease on a synced folder is looked for again a second later;
# tests have no sync to wait for (read at import, so set before dandelion loads).
os.environ.setdefault("DANDELION_STORE_RETRY_S", "0")
os.environ.setdefault("DANDELION_LEASE_RETRY_S", "0")


@pytest.fixture(autouse=True)
def _isolated_user_data(tmp_path, monkeypatch):
    """Real notebooks, papers, keys and tool config live in
    ~/.config/dandelion. Point every test at its own tmp_path so none can
    read or clobber them. Tests may still override any of these themselves."""
    # A throwaway HOME too: startup code that deliberately ignores DANDELION_DATA
    # (e.g. the one-time ~/.config migration) must never reach the real home.
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("DANDELION_DATA", str(tmp_path))
    (tmp_path / ".dandelion-store").write_text("")       # a non-default root needs its marker
    monkeypatch.setenv("DANDELION_PAPERS", str(tmp_path / "papers"))
    monkeypatch.setenv("DANDELION_SECRETS", str(tmp_path / "secrets.json"))
    monkeypatch.setenv("DANDELION_TOOLS", str(tmp_path / "tools.json"))
    from dandelion import datadir
    datadir.unfreeze_root()                      # serve freezes the root; tests look it up afresh
    app = sys.modules.get("dandelion.app")
    if app is not None:
        app.STATE.pop("_target_ready", None)
    yield
    datadir.unfreeze_root()


def pytest_configure(config):
    config.addinivalue_line(
        "markers", "real_auth_probes: keep the real claude/codex auth_status (tests fake subprocess)")


@pytest.fixture(autouse=True)
def _no_real_cli_probes(request, monkeypatch):
    """Settings probes `claude` / `codex`; no test may launch the real binaries.
    Tests of the probes themselves opt out with @pytest.mark.real_auth_probes."""
    from dandelion import claude_cli, codex_cli
    app = sys.modules.get("dandelion.app")
    if app is not None:
        app._clear_auth_cache()                  # cached probes must not leak between tests
    if request.node.get_closest_marker("real_auth_probes"):
        return
    monkeypatch.setattr(claude_cli, "auth_status", lambda *a, **k: {
        "installed": False, "logged_in": False, "plan": "", "email": "",
        "method": "", "provider": ""})
    monkeypatch.setattr(codex_cli, "auth_status", lambda *a, **k: {
        "installed": False, "logged_in": False, "method": ""})
