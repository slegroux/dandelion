"""Folder export: Markdown + image files + .ipynb."""
import base64
import re

from dandelion.client import Msg
from dandelion.export_nb import MARKER, export_folder, to_markdown

PNG = base64.b64encode(b"\x89PNG fake").decode()
JPG = base64.b64encode(b"\xff\xd8 fake").decode()
SVG = "<svg xmlns='http://www.w3.org/2000/svg'></svg>"


def _msgs():
    return [
        Msg("_a", "note", "# Title"),
        Msg("_b", "code", "plot()", output="hi", rich=[
            {"type": "image/png", "data": PNG},
            {"type": "image/jpeg", "data": JPG},
            {"type": "image/svg+xml", "data": SVG},
            {"type": "text/html", "data": "<b>x</b>"}]),
    ]


def test_export_folder_links_resolve_and_extensions(tmp_path):
    md = export_folder(_msgs(), tmp_path / "out", "My Dialog")
    text = md.read_text()
    links = re.findall(r"!\[output\]\(([^)]+)\)", text)
    assert [l.rsplit(".", 1)[1] for l in links] == ["png", "jpg", "svg"]
    assert "data:" not in text
    for l in links:
        assert (md.parent / l).is_file()
    assert "<svg" in (md.parent / "my_dialog_files" / "cell-2-3.svg").read_text()
    assert text.startswith(MARKER + "\n")
    assert (md.parent / "my_dialog.ipynb").is_file()


def test_export_folder_stays_inside_dest(tmp_path):
    work = tmp_path / "work"                     # tmp_path itself is the test data root
    work.mkdir()
    dest = work / "dest"
    export_folder(_msgs(), dest, "../../evil/../name")
    assert {p.name for p in work.iterdir()} == {"dest"}
    assert all(dest in p.parents for p in dest.rglob("*"))


def test_route_writes_folder(monkeypatch, tmp_path):
    import dandelion.app as app
    from starlette.testclient import TestClient
    monkeypatch.setitem(app.STATE, "backend", type("B", (), {"messages": lambda s, d: _msgs()})())
    c = TestClient(app.app)
    dest = tmp_path / "exp"
    r = c.post("/export/folder", data={"dest": str(dest)}, follow_redirects=False)
    assert r.status_code == 303
    assert list(dest.glob("*.md")) and list(dest.glob("*_files/*.png"))


def test_route_rejects_relative_and_uncreatable(monkeypatch, tmp_path):
    import dandelion.app as app
    from starlette.testclient import TestClient
    monkeypatch.setitem(app.STATE, "backend", type("B", (), {"messages": lambda s, d: _msgs()})())
    flashes = []
    monkeypatch.setattr(app, "set_cur", lambda k, v: flashes.append((k, v)))
    c = TestClient(app.app)
    c.post("/export/folder", data={"dest": "relative/dir"}, follow_redirects=False)
    blocker = tmp_path / "file"
    blocker.write_text("x")
    c.post("/export/folder", data={"dest": str(blocker / "sub")}, follow_redirects=False)
    assert "absolute" in flashes[0][1]
    assert "Couldn't write" in flashes[1][1]
    assert not (tmp_path / "relative").exists()


def test_to_markdown_default_is_inline_and_hook_is_used():
    m = _msgs()
    text = to_markdown(m)
    assert f"![output](data:image/png;base64,{PNG})" in text
    assert f"![output](data:image/svg+xml;base64,{SVG})" in text
    assert text == "# Title\n\n```python\nplot()\n```\n\n```\nhi\n```\n\n" + "\n\n".join(
        f"![output](data:{t};base64,{d})" for t, d in
        [("image/png", PNG), ("image/jpeg", JPG), ("image/svg+xml", SVG)]) + "\n"
    assert "X1" in to_markdown(m, lambda n, k, item: "X1")


def test_refuses_foreign_file_even_case_insensitively(tmp_path):
    import pytest
    work = tmp_path / "work"                     # tmp_path itself is the test data root
    work.mkdir()
    (work / "README.md").write_text("ORIGINAL")
    with pytest.raises(ValueError, match="already exists"):
        export_folder(_msgs(), work, "readme")
    assert (work / "README.md").read_text() == "ORIGINAL"
    assert [p.name for p in work.iterdir()] == ["README.md"]


def test_refuses_foreign_ipynb(tmp_path):
    import pytest
    (tmp_path / "d.ipynb").write_text("{}")
    with pytest.raises(ValueError):
        export_folder(_msgs(), tmp_path, "d")


def test_reexport_overwrites_and_drops_stale_images(tmp_path):
    md = export_folder(_msgs(), tmp_path, "d")
    stale = md.parent / "d_files" / "cell-9-1.png"
    stale.write_bytes(b"x")
    other = md.parent / "d_files" / "keep.txt"
    other.write_text("k")
    export_folder(_msgs()[:1], tmp_path, "d")
    assert not stale.exists() and not (md.parent / "d_files" / "cell-2-1.png").exists()
    assert other.exists()
    assert md.read_text().startswith(MARKER)


def test_bad_image_skipped_no_partial_folder(tmp_path):
    bad = [Msg("_a", "code", "x", rich=[{"type": "image/png", "data": "not base64!"},
                                        {"type": "image/svg+xml", "data": ["list"]}])]
    md = export_folder(bad, tmp_path / "o", "d")
    assert md.read_text().count("*[image not exported]*") == 2
    assert not (tmp_path / "o" / "d_files").exists()


def test_svg_is_sanitized(tmp_path):
    m = [Msg("_a", "code", "x", rich=[{"type": "image/svg+xml",
          "data": "<svg xmlns='http://www.w3.org/2000/svg'><script>alert(1)</script>"
                  "<rect width='1' height='1' onload='x()'/></svg>"}])]
    md = export_folder(m, tmp_path, "d")
    svg = (md.parent / "d_files" / "cell-1-1.svg").read_text()
    assert "script" not in svg and "onload" not in svg and "<rect" in svg


def test_symlinked_files_dir_rejected(tmp_path):
    import pytest
    out = tmp_path / "out"
    out.mkdir()
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    (out / "d_files").symlink_to(elsewhere)
    with pytest.raises(ValueError, match="outside"):
        export_folder(_msgs(), out, "d")
    assert list(elsewhere.iterdir()) == []


def test_route_nul_in_dest_flashes(monkeypatch):
    import dandelion.app as app
    from starlette.testclient import TestClient
    monkeypatch.setitem(app.STATE, "backend", type("B", (), {"messages": lambda s, d: _msgs()})())
    flashes = []
    monkeypatch.setattr(app, "set_cur", lambda k, v: flashes.append(v))
    r = TestClient(app.app).post("/export/folder", data={"dest": "/tmp/a\x00b"},
                                 follow_redirects=False)
    assert r.status_code == 303 and "Couldn't write" in flashes[0]


def _route(monkeypatch, picker, client=("testclient", 50000)):
    """POST /export/folder with no dest, the OS picker replaced by `picker`."""
    import dandelion.app as app
    from starlette.testclient import TestClient
    monkeypatch.setitem(app.STATE, "backend", type("B", (), {"messages": lambda s, d: _msgs()})())
    flashes = []
    monkeypatch.setattr(app, "set_cur", lambda k, v: flashes.append(v))
    monkeypatch.setattr(app, "_choose_folder", picker)
    r = TestClient(app.app, client=client).post("/export/folder", data={}, follow_redirects=False)
    return r, flashes


def test_route_without_dest_exports_into_the_folder_the_picker_returns(monkeypatch, tmp_path):
    r, flashes = _route(monkeypatch, lambda: str(tmp_path / "chosen"))
    assert r.status_code == 303 and flashes[0].startswith("Exported to")
    assert list((tmp_path / "chosen").glob("*.md")) and list((tmp_path / "chosen").glob("*_files/*.png"))


def test_cancelling_the_picker_writes_and_says_nothing(monkeypatch, tmp_path):
    r, flashes = _route(monkeypatch, lambda: "")
    assert r.status_code == 303 and flashes == []


def test_picker_failure_flashes_the_reason(monkeypatch):
    def picker():
        raise ValueError("the folder picker needs macOS")
    r, flashes = _route(monkeypatch, picker)
    assert "needs macOS" in flashes[0]


def test_remote_peer_never_opens_the_picker(monkeypatch):
    import dandelion.app as app
    monkeypatch.setattr(app, "_TRUST_TAILNET", True)      # a tailnet peer passes the guard...
    called = []
    r, flashes = _route(monkeypatch, lambda: called.append(1) or "/tmp/x",
                        client=("100.64.0.5", 50000))     # ...but must not get a dialog here
    assert called == [] and "machine running Dandelion" in flashes[0]
