import sys
from pathlib import Path

import pytest

# Make `import pappus` / `import server` work for any test file run alone
# (uv runs the project unpackaged, `package = false`).
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


@pytest.fixture(autouse=True)
def _isolated_user_data(tmp_path, monkeypatch):
    """Real notebooks, papers, keys and tool config live in
    ~/.config/pappus. Point every test at its own tmp_path so none can
    read or clobber them. Tests may still override any of these themselves."""
    # A throwaway HOME too: startup code that deliberately ignores PAPPUS_DATA
    # (e.g. the one-time ~/.config migration) must never reach the real home.
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("PAPPUS_DATA", str(tmp_path))
    monkeypatch.setenv("PAPPUS_PAPERS", str(tmp_path / "papers"))
    monkeypatch.setenv("PAPPUS_SECRETS", str(tmp_path / "secrets.json"))
    monkeypatch.setenv("PAPPUS_TOOLS", str(tmp_path / "tools.json"))


def pytest_configure(config):
    config.addinivalue_line(
        "markers", "real_auth_probes: keep the real claude/codex auth_status (tests fake subprocess)")


@pytest.fixture(autouse=True)
def _no_real_cli_probes(request, monkeypatch):
    """Settings probes `claude` / `codex`; no test may launch the real binaries.
    Tests of the probes themselves opt out with @pytest.mark.real_auth_probes."""
    from pappus import claude_cli, codex_cli
    app = sys.modules.get("pappus.app")
    if app is not None:
        app._clear_auth_cache()                  # cached probes must not leak between tests
    if request.node.get_closest_marker("real_auth_probes"):
        return
    monkeypatch.setattr(claude_cli, "auth_status", lambda *a, **k: {
        "installed": False, "logged_in": False, "plan": "", "email": "",
        "method": "", "provider": ""})
    monkeypatch.setattr(codex_cli, "auth_status", lambda *a, **k: {
        "installed": False, "logged_in": False, "method": ""})
