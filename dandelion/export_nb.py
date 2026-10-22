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
from pathlib import Path


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


def to_markdown(msgs) -> str:
    """Export cells to a single Markdown document."""
    out = []
    for m in msgs:
        if m.msg_type == "code":
            out.append(f"```python\n{m.content}\n```")
            if m.output:
                out.append(f"```\n{m.output}\n```")
            for item in m.rich:
                if item.get("type", "").startswith("image/"):
                    out.append(f"![output](data:{item['type']};base64,{item['data']})")
        elif m.msg_type == "note":
            out.append(m.content)
        else:
            out.append(_prompt_md(m))
    return "\n\n".join(out) + "\n"


_IMAGE_EXT = {"image/png": "png", "image/jpeg": "jpg", "image/svg+xml": "svg"}


def _image_bytes(mime: str, data) -> bytes:
    if mime == "image/svg+xml":                    # SVG is text, the others base64
        return data.encode("utf-8") if isinstance(data, str) else bytes(data)
    return base64.b64decode(data)


def export_folder(msgs, dest, name: str = "dialog") -> Path:
    """Write ``<slug>.md`` (images saved under ``<slug>_files/`` and linked relatively)
    and ``<slug>.ipynb`` into ``dest``, creating it if missing. Returns the .md path.

    The slug is built from ``name`` so every written name is a safe identifier:
    nothing is ever written outside ``dest``."""
    from .export import slug
    stem = slug(name)
    dest = Path(dest)
    files_dir = dest / f"{stem}_files"
    out, made = [], False
    for n, m in enumerate(msgs, 1):
        if m.msg_type == "code":
            out.append(f"```python\n{m.content}\n```")
            if m.output:
                out.append(f"```\n{m.output}\n```")
            k = 0
            for item in m.rich:
                ext = _IMAGE_EXT.get(item.get("type", ""))
                if not ext:
                    continue
                k += 1
                if not made:
                    files_dir.mkdir(parents=True, exist_ok=True)
                    made = True
                fname = f"cell-{n}-{k}.{ext}"
                (files_dir / fname).write_bytes(_image_bytes(item["type"], item.get("data", "")))
                out.append(f"![output]({stem}_files/{fname})")
        elif m.msg_type == "note":
            out.append(m.content)
        else:
            out.append(_prompt_md(m))
    dest.mkdir(parents=True, exist_ok=True)
    md_path = dest / f"{stem}.md"
    md_path.write_text("\n\n".join(out) + "\n", encoding="utf-8")
    (dest / f"{stem}.ipynb").write_text(json.dumps(to_ipynb(msgs), indent=1), encoding="utf-8")
    return md_path
