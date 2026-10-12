# Brand assets

`og.html` is the source of the 1200×630 social preview, `site/assets/og.png`.
It is self-contained (Source Serif 4 comes from Google Fonts, so render online).

Re-render from the repo root:

```bash
CHROME=$(find ~/Library/Caches/ms-playwright/chromium_headless_shell-* -name chrome-headless-shell -type f | head -1)
"$CHROME" --headless --hide-scrollbars --window-size=1200,630 \
  --virtual-time-budget=10000 --screenshot=site/assets/og.png \
  "file://$PWD/docs/brand/og.html"
```

Product screenshots (`site/assets/shot-*.jpg`) are produced from reproducible demo
data: see `scripts/demo_data.py` and `scripts/shots.py`.
