"""Settings separates subscription sign-in (claude / codex CLI) from API keys.

The CLI probes are faked: no test runs a real `claude` or `codex` binary."""
import subprocess
from types import SimpleNamespace

import pytest

from pappus import claude_cli, codex_cli


def _run_returning(stdout="", stderr="", returncode=0, calls=None):
    def run(argv, **kw):
        if calls is not None:
            calls.append((argv, kw))
        return SimpleNamespace(stdout=stdout, stderr=stderr, returncode=returncode)
    return run


# --- claude_cli.auth_status ---------------------------------------------------

def test_claude_status_not_installed(monkeypatch):
    monkeypatch.setattr(claude_cli, "claude_bin", lambda: None)
    assert claude_cli.auth_status() == {"installed": False, "logged_in": False,
                                        "plan": "", "email": ""}


def test_claude_status_signed_in_reads_plan_and_email(monkeypatch):
    calls = []
    monkeypatch.setattr(claude_cli, "claude_bin", lambda: "/bin/claude")
    monkeypatch.setattr(subprocess, "run", _run_returning(
        '{"loggedIn": true, "subscriptionType": "max", "email": "a@b.c"}', calls=calls))
    st = claude_cli.auth_status()
    assert st == {"installed": True, "logged_in": True, "plan": "max", "email": "a@b.c"}
    assert calls[0][0] == ["/bin/claude", "auth", "status"]


def test_claude_status_probe_hides_api_key(monkeypatch):
    # An exported key must not make a logged-out CLI look signed in.
    calls = []
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-secret")
    monkeypatch.setattr(claude_cli, "claude_bin", lambda: "/bin/claude")
    monkeypatch.setattr(subprocess, "run", _run_returning('{"loggedIn": false}', calls=calls))
    assert claude_cli.auth_status()["logged_in"] is False
    assert "ANTHROPIC_API_KEY" not in calls[0][1]["env"]


@pytest.mark.parametrize("exc", [subprocess.TimeoutExpired("claude", 5), OSError("boom")])
def test_claude_status_unknown_on_probe_failure(monkeypatch, exc):
    def run(*a, **k):
        raise exc
    monkeypatch.setattr(claude_cli, "claude_bin", lambda: "/bin/claude")
    monkeypatch.setattr(subprocess, "run", run)
    assert claude_cli.auth_status()["logged_in"] is None


def test_claude_status_unknown_on_garbage_output(monkeypatch):
    monkeypatch.setattr(claude_cli, "claude_bin", lambda: "/bin/claude")
    monkeypatch.setattr(subprocess, "run", _run_returning("not json"))
    assert claude_cli.auth_status()["logged_in"] is None


# --- codex_cli.auth_status ----------------------------------------------------

def test_codex_status_not_installed(monkeypatch):
    monkeypatch.setattr(codex_cli, "codex_bin", lambda: None)
    assert codex_cli.auth_status() == {"installed": False, "logged_in": False, "method": ""}


@pytest.mark.parametrize("out,code,logged_in,method", [
    ("Logged in using ChatGPT\n", 0, True, "chatgpt"),
    ("Logged in using an API key - sk-proj-***abcd\n", 0, True, "api_key"),
    ("Not logged in\n", 1, False, ""),
])
def test_codex_status_parses_login_status(monkeypatch, out, code, logged_in, method):
    monkeypatch.setattr(codex_cli, "codex_bin", lambda: "/bin/codex")
    monkeypatch.setattr(subprocess, "run", _run_returning(out, returncode=code))
    st = codex_cli.auth_status()
    assert (st["logged_in"], st["method"]) == (logged_in, method)
    assert "sk-" not in str(st)                      # never echo key material


# --- SettingsPage -------------------------------------------------------------

def _render(monkeypatch, claude, codex):
    from fasthtml.common import to_xml

    from pappus import app
    monkeypatch.setattr(app.claude_cli, "auth_status", lambda: claude)
    monkeypatch.setattr(app.codex_cli, "auth_status", lambda: codex)
    return to_xml(app.SettingsPage())


NO_CODEX = {"installed": False, "logged_in": False, "method": ""}


def test_settings_shows_both_sections(monkeypatch):
    html = _render(monkeypatch, {"installed": True, "logged_in": True,
                                 "plan": "max", "email": "a@b.c"}, NO_CODEX)
    assert "Subscription" in html and "API keys" in html and "billed per token" in html
    assert "signed in · Max plan · a@b.c" in html
    assert "codex CLI not installed" in html
    assert "<optgroup" in html


def test_settings_logged_out_shows_sign_in_command(monkeypatch):
    html = _render(monkeypatch, {"installed": True, "logged_in": False,
                                 "plan": "", "email": ""}, NO_CODEX)
    assert "not signed in" in html and "claude auth login" in html


def test_default_model_picker_groups_by_billing(monkeypatch):
    from pappus import app
    assert app._is_subscription_model("claude-opus-high")
    assert app._is_subscription_model("codex-gpt-5.5-high")
    assert not app._is_subscription_model("claude")      # Anthropic API
    assert not app._is_subscription_model("glm")
