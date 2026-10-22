"""Images in the Claude CLI route: collection, the stream-json turn, resume delta.

The CLI is never run: _run_cli/_popen are replaced by recorders.
"""
import json
from types import SimpleNamespace

import pytest

import dandelion.claude_cli as cc
from dandelion.client import Msg, collect_images, MAX_CONTEXT_IMAGES, MAX_IMAGE_BYTES

PNG = "iVBORw0KGgo="


def _cell(i, rich, **kw):
    return Msg(id=f"c{i}", msg_type="code", content=f"x{i}", output="", rich=rich, **kw)


def _img(data=PNG, mime="image/png"):
    return {"type": mime, "data": data}


@pytest.fixture
def cli(monkeypatch):
    monkeypatch.setattr(cc, "claude_bin", lambda: "/bin/claude")
    monkeypatch.delenv("DANDELION_CLAUDE_CLI_MODEL", raising=False)
    runs = []

    def run(cmd, cwd, env, timeout, stdin=None):
        runs.append({"cmd": cmd, "stdin": stdin})
        lines = [{"type": "system"},
                 {"type": "result", "session_id": "sid-i", "is_error": False, "result": "ok"}]
        return SimpleNamespace(returncode=0, stderr="",
                               stdout="\n".join(json.dumps(x) for x in lines))
    monkeypatch.setattr(cc, "_run_cli", run)
    return runs


def _blocks(run):
    return json.loads(run["stdin"])["message"]["content"]


def test_collect_images_skips_muted_svg_oversize_and_caps_count():
    msgs = [_cell(0, [_img()], muted=True), _cell(1, [_img("a")]),
            _cell(2, [_img(mime="image/svg+xml")]),
            _cell(3, [_img("A" * (MAX_IMAGE_BYTES * 4 // 3 + 8))])]
    imgs, notes = collect_images(msgs)
    assert [i["id"] for i in imgs] == ["c1"]
    assert any("SVG" in n for n in notes) and any("over" in n for n in notes)

    many = [_cell(n, [_img(f"d{n}")]) for n in range(MAX_CONTEXT_IMAGES + 2)]
    many[0].pinned = True
    imgs, notes = collect_images(many)
    assert len(imgs) == MAX_CONTEXT_IMAGES
    assert imgs[0]["id"] == "c0" and imgs[-1]["id"] == f"c{MAX_CONTEXT_IMAGES + 1}"
    assert any("older image" in n for n in notes)
    assert [i["id"] for i in collect_images(many, upto_id="c3")[0]] == ["c0", "c1", "c2"]


def test_image_turn_is_stream_json_on_stdin(cli):
    cc.CLI_SESSIONS.pop("img/a", None)
    imgs, _ = collect_images([_cell(0, [_img()])])
    out = cc.call("img/a", "which?", context="<code>x0</code>", images=imgs,
                  image_notes=["cell 3: SVG image not sent (no rasterizer available)"])
    assert out == "ok"
    cmd = cli[-1]["cmd"]
    assert cmd[cmd.index("--output-format") + 1] == "stream-json" and "--verbose" in cmd
    assert cmd[cmd.index("--input-format") + 1] == "stream-json"
    assert "which?" not in cmd                                  # no positional prompt
    blocks = _blocks(cli[-1])
    assert "which?" in blocks[0]["text"] and "SVG image not sent" in blocks[0]["text"]
    assert blocks[2] == {"type": "image", "source": {
        "type": "base64", "media_type": "image/png", "data": PNG}}
    assert cc.CLI_SESSIONS["img/a"]["sent_images"] == [("c0", imgs[0]["hash"])]


def test_resume_sends_only_new_images_and_muted_image_restarts(cli):
    cc.CLI_SESSIONS.pop("img/b", None)
    c0, c1 = _cell(0, [_img("aa")]), _cell(1, [_img("bb")])
    ctx = "<code>x0</code>"
    cc.call("img/b", "q1", ctx, images=collect_images([c0])[0])
    ctx2 = ctx + "\n<code>x1</code>"
    cc.call("img/b", "q2", ctx2, images=collect_images([c0, c1])[0])
    cmd = cli[-1]["cmd"]
    assert "--resume" in cmd
    images = [b for b in _blocks(cli[-1]) if b["type"] == "image"]
    assert [b["source"]["data"] for b in images] == ["bb"]       # only the new one

    c0.muted = True                                              # image above is gone
    cc.call("img/b", "q3", "<code>x1</code>", images=collect_images([c0, c1])[0])
    cmd = cli[-1]["cmd"]
    assert "--session-id" in cmd and "--resume" not in cmd
    assert [b["source"]["data"] for b in _blocks(cli[-1]) if b["type"] == "image"] == ["bb"]


def test_resume_with_nothing_new_keeps_the_positional_path(cli):
    cc.CLI_SESSIONS.pop("img/c", None)
    imgs = collect_images([_cell(0, [_img()])])[0]
    cc.call("img/c", "q1", "<code>x0</code>", images=imgs)
    cc.call("img/c", "q2", "<code>x0</code>\n<code>y</code>", images=imgs)
    cmd = cli[-1]["cmd"]
    assert "--resume" in cmd and "--input-format" not in cmd
    assert cli[-1]["stdin"] is None and "y" in cmd[-1]


def test_text_only_turn_is_unchanged(cli, monkeypatch):
    cc.CLI_SESSIONS.pop("img/d", None)
    monkeypatch.setattr(cc, "_run_cli", lambda cmd, cwd, env, timeout: (
        cli.append({"cmd": cmd}) or SimpleNamespace(
            returncode=0, stderr="", stdout=json.dumps({"result": "t", "session_id": "s"}))))
    assert cc.call("img/d", "hi", "ctx") == "t"
    cmd = cli[-1]["cmd"]
    assert cmd[-1] == "hi" and "--input-format" not in cmd
    assert cmd[cmd.index("--output-format") + 1] == "json"
    assert cc.CLI_SESSIONS["img/d"]["sent_images"] == []


def test_old_session_without_sent_images_counts_as_none_sent(cli):
    cc.CLI_SESSIONS["img/e"] = {"id": "old", "sent": "<code>x0</code>", "mode": None,
                                "model": None, "effort": None}
    imgs = collect_images([_cell(0, [_img()])])[0]
    cc.call("img/e", "q", "<code>x0</code>\n<code>y</code>", images=imgs)
    assert "--resume" in cli[-1]["cmd"]
    assert len([b for b in _blocks(cli[-1]) if b["type"] == "image"]) == 1


def test_stream_writes_the_turn_to_stdin(monkeypatch):
    monkeypatch.setattr(cc, "claude_bin", lambda: "/bin/claude")
    cc.CLI_SESSIONS.pop("img/s", None)
    got = []

    def popen(cmd, cwd, env, stdin=None):
        got.append((cmd, stdin))
        ev = [{"type": "stream_event", "event": {"type": "content_block_delta",
                                                 "delta": {"text": "hey"}}},
              {"type": "result", "session_id": "sid-s", "is_error": False}]
        it = iter(json.dumps(e) + "\n" for e in ev)
        return SimpleNamespace(stdout=SimpleNamespace(readline=lambda: next(it, "")),
                               wait=lambda timeout=None: 0)
    monkeypatch.setattr(cc, "_popen", popen)
    imgs = collect_images([_cell(0, [_img()])])[0]
    assert list(cc.stream("img/s", "q", "<code>x0</code>", images=imgs)) == ["hey"]
    cmd, stdin = got[0]
    assert cmd[cmd.index("--input-format") + 1] == "stream-json" and "q" not in cmd
    assert json.loads(stdin)["message"]["content"][-1]["type"] == "image"
    assert cc.CLI_SESSIONS["img/s"]["sent_images"][0][0] == "c0"
