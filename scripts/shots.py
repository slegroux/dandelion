"""Render site/assets/shot-{light,dark,paper}.jpg from a running demo UI.

Start the scratch kernel + UI first (see scripts/demo_data.py), then:

    uvx --with playwright python scripts/shots.py --base http://localhost:8100 --out site/assets

Uses the headless Chromium already in ~/Library/Caches/ms-playwright. 1440x900 at
device scale 2 -> 2880x1800, saved as JPEG.
"""
from __future__ import annotations

import argparse
import glob
import io
import os
import sys
import time
from pathlib import Path

from playwright.sync_api import sync_playwright

DIALOG = "fourier/square-wave"
ARTICLE = "https://en.wikipedia.org/wiki/Fourier_series"


def chrome() -> str:
    hits = sorted(glob.glob(os.path.expanduser(
        "~/Library/Caches/ms-playwright/chromium_headless_shell-*/**/chrome-headless-shell"), recursive=True))
    if not hits:
        sys.exit("no chrome-headless-shell found")
    return hits[0]


def jpeg(page, path: Path, quality: int) -> None:
    from PIL import Image
    png = page.screenshot(type="png")
    Image.open(io.BytesIO(png)).convert("RGB").save(path, "JPEG", quality=quality, optimize=True)
    print("wrote", path)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="http://localhost:8100")
    ap.add_argument("--out", type=Path, default=Path("site/assets"))
    ap.add_argument("--quality", type=int, default=82)
    a = ap.parse_args()
    with sync_playwright() as p:
        b = p.chromium.launch(executable_path=chrome())

        def ctx(scheme: str):
            c = b.new_context(viewport={"width": 1440, "height": 900}, device_scale_factor=2, color_scheme=scheme)
            pg = c.new_page()
            pg.goto(f"{a.base}/paper/close")          # the open paper is server-side state: start without it
            pg.goto(f"{a.base}/open?dialog={DIALOG}")
            pg.wait_for_load_state("networkidle")
            pg.evaluate("() => document.querySelectorAll('details.folder').forEach(f => f.open = true)")
            return c, pg

        for scheme in ("light", "dark"):
            c, pg = ctx(scheme)
            pg.evaluate("""() => { for (const el of document.querySelectorAll('*'))
                if (el.scrollHeight > el.clientHeight + 50 && /(auto|scroll)/.test(getComputedStyle(el).overflowY)
                    && el.clientWidth > 600) el.scrollTop = el.scrollHeight; }""")
            pg.wait_for_timeout(400)
            jpeg(pg, a.out / f"shot-{scheme}.jpg", a.quality)
            c.close()

        c, pg = ctx("light")
        pg.evaluate("""async (u) => { const f = new FormData(); f.append('url', u);
            await fetch('/paper/open', {method: 'POST', body: f}); }""", ARTICLE)
        for _ in range(60):
            time.sleep(1)
            if "ready" in pg.evaluate("() => fetch('/paper/status').then(r => r.text())"):
                break
        pg.goto(f"{a.base}/open?dialog={DIALOG}")
        pg.wait_for_load_state("networkidle")
        pg.evaluate("() => document.querySelectorAll('details.folder').forEach(f => f.open = true)")
        for sel in ("#tgl-side", "#tgl-toc"):      # the paper shot hides the sidebar and the contents rail
            pg.click(sel)
        pg.wait_for_timeout(500)
        pg.evaluate("""() => { for (const el of document.querySelectorAll('*'))
            if (el.scrollHeight > el.clientHeight + 200 && el.clientWidth > 500 && el.clientWidth < 900
                && /(auto|scroll)/.test(getComputedStyle(el).overflowY) && el.textContent.includes('series expansion'))
                el.scrollTop = 85; }""")
        pg.wait_for_timeout(300)
        jpeg(pg, a.out / "shot-paper.jpg", a.quality)
        c.close()
        b.close()


if __name__ == "__main__":
    main()
