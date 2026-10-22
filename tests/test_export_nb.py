"""Folder export: Markdown + image files + .ipynb."""
import base64
import re

from dandelion.client import Msg
from dandelion.export_nb import export_folder

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
    assert (md.parent / "my_dialog_files" / "cell-2-3.svg").read_text() == SVG
    assert (md.parent / "my_dialog.ipynb").is_file()


def test_export_folder_stays_inside_dest(tmp_path):
    dest = tmp_path / "dest"
    export_folder(_msgs(), dest, "../../evil/../name")
    assert {p.name for p in tmp_path.iterdir()} == {"dest"}
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
