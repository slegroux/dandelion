"""Images and the Codex route: it is text-only, so the turn and the reply say so.

No CLI is run: the stream functions are replaced by recorders.
"""
import asyncio

from dandelion import app
from dandelion import codex_cli as cx

PNG = "iVBORw0KGgo="


def _run_route(monkeypatch, model, rich, tool_only=False):
    d = f"cximg/{model}-{len(rich)}-{tool_only}-{id(rich)}"
    app.STATE["dialog"] = d
    b = app.STATE["backend"]
    b.messages(d)
    cell = b.add(d, "plot()", "code")
    cell.rich = rich
    m = b.add(d, "what is in the plot?", "prompt", model=model)
    app.STATE["pending_stream"] = (d, m.id)
    seen = {}

    def codex(dialog, content, ctx, model=None, mode=None, image_note=""):
        seen["codex"] = image_note
        if tool_only:                       # the AI edited cells and said nothing
            app._mark_cells_dirty()
            return iter([])
        return iter(["ans"])

    def claude(dialog, content, ctx, model=None, mode=None, images=None, image_notes=None):
        seen["claude"] = images
        return iter(["ans"])

    monkeypatch.setattr(app, "stream_codex", codex)
    monkeypatch.setattr(app, "stream_claude", claude)
    resp = app.stream_answer(dialog=d, id=m.id)

    async def collect():
        return "".join([c.decode() if isinstance(c, bytes) else c
                        async for c in resp.body_iterator])
    asyncio.run(collect())
    return seen, m


def test_codex_route_with_images_says_it_cannot_send_them(monkeypatch):
    seen, m = _run_route(monkeypatch, "codex-gpt-5.5-high",
                         [{"type": "image/png", "data": PNG}])
    assert seen["codex"] == "1 image output(s) not sent: the Codex route is text-only"
    assert "claude" not in seen
    assert m.output.startswith("_1 image output(s) not sent")
    assert m.output.endswith("ans")


def test_tool_only_codex_turn_with_images_still_gets_the_updated_line(monkeypatch):
    _, m = _run_route(monkeypatch, "codex-gpt-5.5-high",
                      [{"type": "image/png", "data": PNG}], tool_only=True)
    assert m.output.startswith("_1 image output(s) not sent")
    assert m.output.endswith("_Updated the notebook cells as requested._")


def test_codex_route_without_images_has_no_note(monkeypatch):
    seen, m = _run_route(monkeypatch, "codex-gpt-5.5-high", [])
    assert seen["codex"] == ""
    assert m.output == "ans"


def test_claude_route_still_gets_the_images(monkeypatch):
    seen, m = _run_route(monkeypatch, "claude-cli-opus-high",
                         [{"type": "image/png", "data": PNG}])
    assert len(seen["claude"]) == 1 and "codex" not in seen
    assert m.output == "ans"


def test_note_reaches_the_codex_turn_text(monkeypatch):
    monkeypatch.setattr(cx, "codex_bin", lambda: "/usr/bin/codex")
    monkeypatch.setenv("DANDELION_CELL_TOOLS", "0")
    cx.CODEX_SESSIONS.pop("cximg/t", None)
    note = "1 image output(s) not sent: the Codex route is text-only"
    with_note = cx._build_cmd_info("cximg/t", "q?", "<code>x</code>", None, True, image_note=note)
    assert f"({note})" in with_note.argv[-1]
    plain = cx._build_cmd_info("cximg/t", "q?", "<code>x</code>", None, True)
    assert "image output" not in plain.argv[-1]


def test_note_counts_every_image_output_not_just_the_sent_ones(monkeypatch):
    seen, _ = _run_route(monkeypatch, "codex-gpt-5.5-high",
                         [{"type": "image/png", "data": PNG}] * 7)
    assert seen["codex"].startswith("7 image output(s)")
    seen, _ = _run_route(monkeypatch, "codex-gpt-5.5-high",
                         [{"type": "image/svg+xml", "data": "<svg/>"}])
    assert seen["codex"].startswith("1 image output(s)")


def test_malformed_rich_items_do_not_break_the_stream(monkeypatch):
    for model in ("codex-gpt-5.5-high", "claude-cli-opus-high"):
        for bad in (["not-a-dict", None, {"type": "image/png", "data": PNG}], "oops"):
            _, m = _run_route(monkeypatch, model, bad)
            assert m.output.endswith("ans")
            assert app.STATE.get("pending_stream") != (app.STATE["dialog"], m.id)
            for c in app.STATE["backend"].messages(app.STATE["dialog"]):
                c.rich = []                      # the shared backend must still render
