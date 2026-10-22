"""Images in the Claude CLI route: collection, the stream-json turn, resume delta.

The CLI is never run: _run_cli/_popen are replaced by recorders.
"""
import json
from types import SimpleNamespace

import pytest

import dandelion.claude_cli as cc
from dandelion.client import MAX_CONTEXT_IMAGES, MAX_IMAGE_BYTES, Msg, collect_images

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
    assert any("not sent (limit" in n for n in notes)
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


def _png_b64(w, h):
    import base64
    return base64.b64encode(b"\x89PNG\r\n\x1a\n" + (13).to_bytes(4, "big") + b"IHDR"
                            + w.to_bytes(4, "big") + h.to_bytes(4, "big") + b"\x08\x02\0\0\0").decode()


def _jpeg_b64(w, h):
    import base64
    return base64.b64encode(b"\xff\xd8\xff\xe0\x00\x04ab" + b"\xff\xc0\x00\x11\x08"
                            + h.to_bytes(2, "big") + w.to_bytes(2, "big") + b"\x03" * 10).decode()


def test_images_over_8000_px_are_skipped_with_a_note():
    msgs = [_cell(0, [_img(_png_b64(9000, 10))]),
            _cell(1, [_img(_jpeg_b64(20, 8500), "image/jpeg")]),
            _cell(2, [_img(_png_b64(1568, 900))]),
            _cell(3, [_img(_jpeg_b64(800, 600), "image/jpeg")])]
    imgs, notes = collect_images(msgs)
    assert [i["id"] for i in imgs] == ["c2", "c3"]
    assert any("9000 x 10 px" in n for n in notes) and any("20 x 8500 px" in n for n in notes)


def test_changed_image_bytes_with_same_text_start_a_fresh_session(cli):
    cc.CLI_SESSIONS.pop("img/f", None)
    c0 = _cell(0, [_img("aa")])
    ctx = "<code>x0</code>"                     # text never changes; only the image does
    cc.call("img/f", "q1", ctx, images=collect_images([c0])[0])
    c0.rich = [_img("zz")]                       # cell re-run: same id, new image bytes
    cc.call("img/f", "q2", ctx, images=collect_images([c0])[0])
    cmd = cli[-1]["cmd"]
    assert "--session-id" in cmd and "--resume" not in cmd
    assert [b["source"]["data"] for b in _blocks(cli[-1]) if b["type"] == "image"] == ["zz"]


def test_slid_window_past_the_cap_starts_a_fresh_session(cli):
    cc.CLI_SESSIONS.pop("img/g", None)
    cells = [_cell(n, [_img(f"d{n}")]) for n in range(MAX_CONTEXT_IMAGES + 1)]
    ctx = "<code>x</code>"
    cc.call("img/g", "q1", ctx, images=collect_images(cells[:MAX_CONTEXT_IMAGES])[0])
    cc.call("img/g", "q2", ctx, images=collect_images(cells)[0])      # oldest slides out
    cmd = cli[-1]["cmd"]
    assert "--session-id" in cmd and "--resume" not in cmd
    assert len([b for b in _blocks(cli[-1]) if b["type"] == "image"]) == MAX_CONTEXT_IMAGES


def test_failed_stream_turn_does_not_advance_the_session(monkeypatch):
    monkeypatch.setattr(cc, "claude_bin", lambda: "/bin/claude")
    cc.CLI_SESSIONS.pop("img/h", None)
    cmds = []
    results = iter([{"type": "result", "session_id": "bad", "is_error": True, "result": "400"},
                    {"type": "result", "session_id": "ok", "is_error": False}])

    def popen(cmd, cwd, env, stdin=None):
        cmds.append(cmd)
        it = iter([json.dumps(next(results)) + "\n"])
        return SimpleNamespace(stdout=SimpleNamespace(readline=lambda: next(it, "")),
                               wait=lambda timeout=None: 0)
    monkeypatch.setattr(cc, "_popen", popen)
    imgs = collect_images([_cell(0, [_img()])])[0]
    list(cc.stream("img/h", "q1", "<code>x0</code>", images=imgs))
    assert "img/h" not in cc.CLI_SESSIONS
    list(cc.stream("img/h", "q2", "<code>x0</code>", images=imgs))
    assert "--session-id" in cmds[-1] and "--resume" not in cmds[-1]
    assert cc.CLI_SESSIONS["img/h"]["id"] == "ok"


def test_image_turn_without_a_result_event_is_an_error(monkeypatch):
    monkeypatch.setattr(cc, "claude_bin", lambda: "/bin/claude")
    cc.CLI_SESSIONS.pop("img/i", None)
    monkeypatch.setattr(cc, "_run_cli", lambda *a, **k: SimpleNamespace(
        returncode=0, stderr="", stdout='{"type":"system"}'))
    imgs = collect_images([_cell(0, [_img()])])[0]
    assert "no result event" in cc.call("img/i", "q", "ctx", images=imgs)


def test_notes_are_sent_only_when_the_image_set_changes(cli):
    cc.CLI_SESSIONS.pop("img/n", None)
    imgs = collect_images([_cell(0, [_img()])])[0]
    cc.call("img/n", "q1", "<code>x0</code>", images=imgs, image_notes=["NOTE"])
    cc.call("img/n", "q2", "<code>x0</code>\n<code>y</code>", images=imgs,
            image_notes=["NOTE"])
    assert "NOTE" in _blocks(cli[0])[0]["text"]          # sent with the image set
    assert "NOTE" not in cli[-1]["cmd"][-1]              # not repeated
    cc.call("img/n", "q3", "<code>x0</code>\n<code>y</code>", images=imgs,
            image_notes=["OTHER"])                       # a new note is said even with no new image
    assert "OTHER" in cli[-1]["cmd"][-1]


def test_stream_with_no_result_reports_the_exit(monkeypatch):
    monkeypatch.setattr(cc, "claude_bin", lambda: "/bin/claude")
    cc.CLI_SESSIONS.pop("img/x", None)
    monkeypatch.setattr(cc, "_popen", lambda *a, **k: SimpleNamespace(
        stdout=SimpleNamespace(readline=lambda: ""), wait=lambda timeout=None: 1,
        returncode=1))
    out = list(cc.stream("img/x", "q", "ctx", images=collect_images([_cell(0, [_img()])])[0]))
    assert out == ["[Claude (Max plan): no answer (exit 1)]"]


def test_malformed_rich_is_skipped_and_long_headers_are_measured():
    msgs = [_cell(0, ["x", None, {"type": "image/png"}]), _cell(1, "oops")]
    assert collect_images(msgs) == ([], [])
    import base64
    big = base64.b64encode(b"\xff\xd8\xff\xe1" + (60000).to_bytes(2, "big") + b"E" * 59998
                           + b"\xff\xc0\x00\x11\x08" + (10).to_bytes(2, "big")
                           + (9000).to_bytes(2, "big") + b"\x03" * 10).decode()
    _, notes = collect_images([_cell(0, [_img(big, "image/jpeg")])])
    assert any("9000 x 10" in n for n in notes)
