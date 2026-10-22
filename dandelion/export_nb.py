"""Export a dialog's messages to a Jupyter notebook (.ipynb) or Markdown document.

These are pure serializers: they take a list of message objects (duck-typed on
``id``/``msg_type``/``content``/``output``/``rich``/``model``) and build
nbformat/Markdown. They read no application state, so they live outside the UI
module (``dandelion.app``) and can be unit-tested in isolation.

This is distinct from :mod:`dandelion.export`, which *tangles* a dialog into a
pip-installable package; here we render the dialog as a document.
"""
from __future__ import annotations

import base64
import json
import re
from pathlib import Path

from .export import slug


def _lines(s: str) -> list:
    """nbformat wants source/text as a list of lines (keeping newlines)."""
    return (s or "").splitlines(keepends=True)


def _code_outputs(m) -> list:
    outs = []
    if m.output:
        outs.append({"output_type": "stream", "name": "stdout", "text": _lines(m.output)})
    for item in m.rich:
        t, data = item.get("type", ""), item.get("data", "")
        if t in ("image/png", "image/jpeg"):
            outs.append({"output_type": "display_data", "data": {t: data}, "metadata": {}})
        elif t == "text/html":
            outs.append({"output_type": "display_data",
                         "data": {"text/html": _lines(data)}, "metadata": {}})
    return outs


def _prompt_md(m) -> str:
    md = f"**Prompt:** {m.content}"
    if m.output:
        md += f"\n\n**{m.model or 'AI'}:**\n\n{m.output}"
    return md


def to_ipynb(msgs) -> dict:
    """Export cells to a Jupyter notebook: code→code cells (with outputs/plots),
    notes→markdown, prompts→markdown (question + AI answer)."""
    cells = []
    for m in msgs:
        cid = (m.id or "").lstrip("_") or "cell"      # nbformat cell id (no leading _)
        if m.msg_type == "code":
            cells.append({"id": cid, "cell_type": "code", "metadata": {}, "execution_count": None,
                          "source": _lines(m.content), "outputs": _code_outputs(m)})
        elif m.msg_type == "note":
            cells.append({"id": cid, "cell_type": "markdown", "metadata": {},
                          "source": _lines(m.content)})
        else:
            cells.append({"id": cid, "cell_type": "markdown", "metadata": {},
                          "source": _lines(_prompt_md(m))})
    return {"cells": cells, "nbformat": 4, "nbformat_minor": 5,
            "metadata": {"kernelspec": {"name": "python3", "display_name": "Python 3",
                                        "language": "python"},
                         "language_info": {"name": "python"}}}


def _inline_image(n: int, k: int, item: dict) -> str:
    return f"![output](data:{item['type']};base64,{item['data']})"


def to_markdown(msgs, image=_inline_image) -> str:
    """Export cells to a single Markdown document. ``image(cell_number, k, item)``
    renders the k-th image output of a cell (default: an inline ``data:`` URI)."""
    out = []
    for n, m in enumerate(msgs, 1):
        if m.msg_type == "code":
            out.append(f"```python\n{m.content}\n```")
            if m.output:
                out.append(f"```\n{m.output}\n```")
            k = 0
            for item in m.rich:
                if item.get("type", "").startswith("image/"):
                    k += 1
                    out.append(image(n, k, item))
        elif m.msg_type == "note":
            out.append(m.content)
        else:
            out.append(_prompt_md(m))
    return "\n\n".join(out) + "\n"


_IMAGE_EXT = {"image/png": "png", "image/jpeg": "jpg", "image/svg+xml": "svg"}


MARKER = "<!-- exported by Dandelion -->"
_STALE = re.compile(r"cell-\d+-\d+\.\w+$")


def _image_bytes(mime: str, data) -> bytes:
    if mime == "image/svg+xml":                    # SVG is text, the others base64
        from .blog import _sanitize                # untrusted input, as in the app view
        if not isinstance(data, str):
            raise TypeError("svg data is not text")
        return _sanitize(data).encode("utf-8")
    return base64.b64decode(data, validate=True)


def export_folder(msgs, dest, name: str = "dialog") -> Path:
    """Write ``<slug>.md`` (images saved under ``<slug>_files/`` and linked relatively)
    and ``<slug>.ipynb`` into ``dest``, creating it if missing. Returns the .md path.

    File names derive from the slug of ``name`` and fixed patterns, and every target
    must resolve inside ``dest`` (symlinks that leave it are refused). An existing
    ``<slug>.md``/``.ipynb`` (compared case-insensitively) is only overwritten when the
    ``.md`` carries MARKER, i.e. an earlier export wrote it; otherwise ValueError.
    Images with undecodable data are skipped with a placeholder. Everything is built
    in memory first, so a refusal or bad data never leaves a partial folder."""
    stem = slug(name)
    dest = Path(dest)
    files: dict[str, bytes] = {}

    def write_image(n: int, k: int, item: dict) -> str:
        ext = _IMAGE_EXT.get(item["type"])
        try:
            if not ext:
                raise ValueError("unsupported image type")
            data = _image_bytes(item["type"], item.get("data", ""))
        except (ValueError, TypeError):
            return "*[image not exported]*"
        fname = f"cell-{n}-{k}.{ext}"
        files[fname] = data
        return f"![output]({stem}_files/{fname})"

    md = MARKER + "\n" + to_markdown(msgs, write_image)
    nb = json.dumps(to_ipynb(msgs), indent=1)

    root = dest.resolve()
    existing = {p.name.lower(): p for p in root.iterdir()} if root.is_dir() else {}
    old_md = existing.get(f"{stem}.md")
    ours = old_md is not None and old_md.read_text(errors="replace").startswith(MARKER)
    if not ours and (old_md or f"{stem}.ipynb" in existing):
        raise ValueError(f"{(old_md or existing[f'{stem}.ipynb'])} already exists "
                         "and was not written by a Dandelion export — choose another folder")
    files_dir = root / f"{stem}_files"
    for target in (root / f"{stem}.md", root / f"{stem}.ipynb", files_dir):
        if not target.resolve().is_relative_to(root):
            raise ValueError(f"{target} resolves outside the export folder")

    root.mkdir(parents=True, exist_ok=True)
    if files:
        files_dir.mkdir(exist_ok=True)
    if files_dir.is_dir():
        for old in files_dir.iterdir():
            if _STALE.match(old.name) and old.name not in files:
                old.unlink()
    for fname, data in files.items():
        (files_dir / fname).write_bytes(data)
    (root / f"{stem}.ipynb").write_text(nb, encoding="utf-8")
    md_path = root / f"{stem}.md"
    md_path.write_text(md, encoding="utf-8")
    return md_path
