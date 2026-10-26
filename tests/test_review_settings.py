"""Settings separates subscription sign-in (claude / codex CLI) from API keys.

The CLI probes are faked: no test runs a real `claude` or `codex` binary."""
import subprocess
from types import SimpleNamespace

import pytest

from dandelion import claude_cli, codex_cli

pytestmark = pytest.mark.real_auth_probes     # exercise the real probes, faked subprocess


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
                                        "plan": "", "email": "", "method": "", "provider": ""}


def test_claude_status_signed_in_reads_plan_and_email(monkeypatch):
    calls = []
    monkeypatch.setattr(claude_cli, "claude_bin", lambda: "/bin/claude")
    monkeypatch.setattr(subprocess, "run", _run_returning(
        '{"loggedIn": true, "subscriptionType": "max", "email": "a@b.c",'
        ' "authMethod": "claude.ai", "apiProvider": "firstParty"}', calls=calls))
    st = claude_cli.auth_status()
    assert st == {"installed": True, "logged_in": True, "plan": "max", "email": "a@b.c",
                  "method": "claude.ai", "provider": "firstParty"}
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


@pytest.mark.parametrize("stdout", ["", "  \n", "null", "[]", '"x"', "{}", '{"email": "a@b.c"}'])
def test_claude_status_unknown_on_empty_or_non_dict(monkeypatch, stdout):
    monkeypatch.setattr(claude_cli, "claude_bin", lambda: "/bin/claude")
    monkeypatch.setattr(subprocess, "run", _run_returning(stdout, returncode=1))
    st = claude_cli.auth_status()
    assert st["installed"] is True and st["logged_in"] is None


def test_probes_pass_timeout_cwd_and_devnull_stdin(monkeypatch):
    calls = []
    monkeypatch.setattr(claude_cli, "claude_bin", lambda: "/bin/claude")
    monkeypatch.setattr(codex_cli, "codex_bin", lambda: "/bin/codex")
    monkeypatch.setattr(subprocess, "run", _run_returning('{"loggedIn": true}', calls=calls))
    claude_cli.auth_status()
    codex_cli.auth_status()
    assert len(calls) == 2
    for _argv, kw in calls:
        assert kw["timeout"] and kw["cwd"]
        assert kw["stdin"] is subprocess.DEVNULL
        assert kw["encoding"] == "utf-8" and kw["errors"] == "replace"
    assert calls[0][1]["timeout"] >= 8


# --- codex_cli.auth_status ----------------------------------------------------

def test_codex_status_not_installed(monkeypatch):
    monkeypatch.setattr(codex_cli, "codex_bin", lambda: None)
    assert codex_cli.auth_status() == {"installed": False, "logged_in": False, "method": ""}


@pytest.mark.parametrize("out,code,logged_in,method", [
    ("Logged in using ChatGPT\n", 0, True, "chatgpt"),
    ("Logged in using an API key - sk-proj-***abcd\n", 0, True, "api_key"),
    ("Not logged in\n", 1, False, ""),
    ("error: unrecognized subcommand 'login'\n", 2, None, ""),
])
def test_codex_status_parses_login_status(monkeypatch, out, code, logged_in, method):
    monkeypatch.setattr(codex_cli, "codex_bin", lambda: "/bin/codex")
    monkeypatch.setattr(subprocess, "run", _run_returning(out, returncode=code))
    st = codex_cli.auth_status()
    assert (st["logged_in"], st["method"]) == (logged_in, method)
    assert "sk-" not in str(st)                      # never echo key material


@pytest.mark.parametrize("exc", [subprocess.TimeoutExpired("codex", 5), OSError("boom")])
def test_codex_status_unknown_on_probe_failure(monkeypatch, exc):
    def run(*a, **k):
        raise exc
    monkeypatch.setattr(codex_cli, "codex_bin", lambda: "/bin/codex")
    monkeypatch.setattr(subprocess, "run", run)
    assert codex_cli.auth_status() == {"installed": True, "logged_in": None, "method": ""}


# --- SettingsPage -------------------------------------------------------------

def _render(monkeypatch, claude, codex):
    from fasthtml.common import to_xml

    from dandelion import app
    monkeypatch.setattr(app.claude_cli, "auth_status", lambda: claude)
    monkeypatch.setattr(app.codex_cli, "auth_status", lambda: codex)
    app._clear_auth_cache()
    return to_xml(app.SettingsPage())


NO_CODEX = {"installed": False, "logged_in": False, "method": ""}
MAX = {"installed": True, "logged_in": True, "plan": "max", "email": "a@b.c",
       "method": "claude.ai", "provider": "firstParty"}


def test_settings_shows_both_sections(monkeypatch):
    html = _render(monkeypatch, MAX, NO_CODEX)
    assert "Subscription" in html and "API keys" in html and "billed per token" in html
    assert "signed in · Max plan · a@b.c" in html
    assert "never bill API credits" in html
    assert "If Ask AI reports an expired session, run claude auth login. claude setup-token" in html
    assert "codex CLI not installed" in html
    assert "<optgroup" in html


def test_settings_logged_out_shows_sign_in_command(monkeypatch):
    html = _render(monkeypatch, {**MAX, "logged_in": False, "plan": "", "email": ""}, NO_CODEX)
    assert "not signed in" in html and "claude auth login" in html
    assert "never bill API credits" not in html


def test_settings_unknown_state_has_no_login_command(monkeypatch):
    html = _render(monkeypatch, {**MAX, "logged_in": None, "plan": "", "email": ""}, NO_CODEX)
    assert "couldn&#x27;t read claude CLI" in html or "couldn't read claude CLI" in html
    assert "prov-cmd" not in html
    assert "claude auth status" in html


def test_settings_claude_non_plan_method_warns_without_billing_note(monkeypatch):
    html = _render(monkeypatch, {**MAX, "plan": "", "method": "api_key"}, NO_CODEX)
    assert "signed in with api_key" in html and "not a plan" in html
    assert "never bill API credits" not in html and "signed in · " not in html
    assert 'prov-status warn' in html
    assert "setup-token" not in html


def test_settings_claude_long_lived_token_is_on_plan(monkeypatch):
    # `claude setup-token` reports authMethod "oauth_token" and no plan or email.
    html = _render(monkeypatch, {**MAX, "plan": "", "email": "", "method": "oauth_token"}, NO_CODEX)
    assert "✓ long-lived token set" in html and "not a plan" not in html
    assert "never bill API credits" in html
    assert "run claude setup-token again" in html


def test_settings_codex_api_key_is_warn(monkeypatch):
    html = _render(monkeypatch, MAX, {"installed": True, "logged_in": True, "method": "api_key"})
    assert "signed in with an API key (billed per token, not a plan)" in html
    assert "ChatGPT plan" not in html


def test_settings_escapes_probe_strings(monkeypatch):
    html = _render(monkeypatch, {**MAX, "email": "<script>alert(1)</script>"}, NO_CODEX)
    assert "<script>alert(1)" not in html and "&lt;script&gt;" in html


def test_settings_probes_are_cached(monkeypatch):
    from dandelion import app
    n = {"claude": 0, "codex": 0}

    def claude():
        n["claude"] += 1
        return MAX

    def codex():
        n["codex"] += 1
        return NO_CODEX
    monkeypatch.setattr(app.claude_cli, "auth_status", claude)
    monkeypatch.setattr(app.codex_cli, "auth_status", codex)
    app._clear_auth_cache()
    app.SettingsPage()
    app.SettingsPage()
    assert n == {"claude": 1, "codex": 1}
    app._clear_auth_cache()
    app.SettingsPage()
    assert n == {"claude": 2, "codex": 2}


def test_settings_probes_run_concurrently(monkeypatch):
    import threading

    from dandelion import app
    both = threading.Barrier(2, timeout=5)      # only passes if both probes run at once

    def probe(result):
        def f():
            both.wait()
            return result
        return f
    monkeypatch.setattr(app.claude_cli, "auth_status", probe(MAX))
    monkeypatch.setattr(app.codex_cli, "auth_status", probe(NO_CODEX))
    app._clear_auth_cache()
    assert app._auth_probes() == (MAX, NO_CODEX)


def test_settings_post_round_trip_saves_default_model(monkeypatch):
    from starlette.testclient import TestClient

    from dandelion import app
    monkeypatch.setattr(app.claude_cli, "auth_status", lambda: MAX)
    monkeypatch.setattr(app.codex_cli, "auth_status", lambda: NO_CODEX)
    monkeypatch.setitem(app.STATE, "model", app.STATE["model"])     # restored after
    app._clear_auth_cache()
    client = TestClient(app.app)
    html = client.get("/settings").text
    assert 'name="default_model"' in html and "Default model" in html
    target = next(m["id"] for m in app.list_models() if m["id"] != app.STATE["model"])
    r = client.post("/settings", data={"default_model": target})
    assert r.status_code == 200 and "Saved" in r.text
    assert app.STATE["model"] == target
    assert f'value="{target}" selected' in r.text or f'selected value="{target}"' in r.text


def test_default_model_picker_groups_by_billing():
    from dandelion import app
    assert app._is_subscription_model("claude-opus-high")
    assert app._is_subscription_model("codex-gpt-5.5-high")
    assert not app._is_subscription_model("claude")      # Anthropic API
    assert not app._is_subscription_model("glm")
