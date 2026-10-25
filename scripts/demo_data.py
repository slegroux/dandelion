"""Build the demo notebook used for the site screenshots, reproducibly.

    uv run --extra kernel python -m server.kernel_server --port 5155 &   # a scratch kernel
    uv run python scripts/demo_data.py /path/to/scratch/dir --kernel-url http://localhost:5155

Writes the notebook (`dialogs-kernel.d/`, one file per dialog, as HttpKernelBackend
persists it) under
<dir>/data, plus <dir>/targets.yaml pointing the `kernel` target at the scratch
kernel. Notes and Ask-AI answers are pre-written (no AI call is made); code cells
are really executed on the kernel so their output and matplotlib figure are real.
Nothing outside <dir> is touched, and an existing notebook there is
replaced only if this script wrote it (or with --force). Run it from a checkout
without a data/ folder: `serve` moves an old <repo>/data into DANDELION_DATA.
Then run the UI with:

    DANDELION_DATA=<dir>/data DANDELION_PAPERS=<dir>/papers DANDELION_SECRETS=<dir>/secrets.json \
    DANDELION_TOOLS=<dir>/tools.json DANDELION_CONFIG=<dir>/targets.yaml \
    DANDELION_TARGET=kernel DANDELION_PORT=8100 uv run python -m dandelion.cli serve
"""
from __future__ import annotations

import argparse
import os
import re
import shutil
import sys
from pathlib import Path

MODEL = "claude-opus-high"

WAVE_Q = "Why does a square wave only have odd harmonics? Don't give me the answer, help me see it."

WAVE_A1 = """Good question to work out yourself, so I won't hand you the proof. Two nudges:

1. **Look at the second half of one period.** How does it compare with the first half? Write that as an equation in `f(t)` and `f(t + T/2)`.
2. **Now take one harmonic, `sin(2πk f₀ t)`, and shift it by half a period.** What happens to it when `k` is even? When `k` is odd?

If the wave has the property from (1), which harmonics can survive it?

You can check your guess numerically before reasoning it out: build one second of a 100 Hz square wave and print the FFT magnitude at the first few multiples of 100 Hz. Predict the numbers first."""

WAVE_CODE3 = '''import numpy as np

sr, f0 = 8000, 100                       # 1 s of a 100 Hz square wave
t = np.arange(sr) / sr
sq = np.sign(np.sin(2*np.pi*f0*t))

mag = np.abs(np.fft.rfft(sq)) / (sr/2)
for k in range(1, 8):
    print(f"harmonic {k}:  {mag[k*f0]:.3f}")'''

WAVE_NOTE4 = """My guess: the second half of the period is the first half flipped, `f(t + T/2) = -f(t)`. An even harmonic repeats after half a period, so it can't flip sign. Only odd ones can."""

WAVE_Q5 = "Here are the numbers: 1.273, 0.423, 0.251, 0.182 for the odd harmonics and about 0.001 for the even ones. Is my note the whole argument?"

WAVE_A5 = """Your numbers match: the odd harmonics (1.273, 0.423, 0.251, 0.182) track `4/(πk)` = 1.273, 0.424, 0.255, 0.182, and the even ones are 0.001, which is sampling noise from a wave that jumps between samples.

Your note is the whole argument. `f(t + T/2) = -f(t)` is *half-wave symmetry*; an even harmonic repeats after half a period and can't change sign, so its coefficient must cancel.

Next step, still yours to run: plot partial sums with 1, 3 and 9 odd terms against the square wave. Before you look, what do you expect near the jumps?"""

WAVE_CODE6 = '''import matplotlib.pyplot as plt

x = np.linspace(0, 2/f0, 800)
fig, ax = plt.subplots(figsize=(7, 2.6))
ax.plot(x*1e3, np.sign(np.sin(2*np.pi*f0*x)), lw=2, label="square")
for n in (1, 3, 9):                       # partial sums: odd harmonics only
    s = sum(4/(np.pi*k)*np.sin(2*np.pi*k*f0*x) for k in range(1, n+1, 2))
    ax.plot(x*1e3, s, lw=1.4, label=f"{n} term" + ("s" if n > 1 else ""))
ax.set_xlabel("ms"); ax.legend(ncol=4, frameon=False);'''

# Run silently in the square-wave kernel namespace (not stored as a cell): the house
# matplotlib look the screenshots use (open axes, muted greys, a four-colour cycle,
# legend centred above the axes).
STYLE = '''
import matplotlib; matplotlib.use("Agg")
import matplotlib.pyplot as plt
from cycler import cycler
plt.rcParams.update({
    "axes.spines.top": False, "axes.spines.right": False, "axes.edgecolor": "#BBBBBB",
    "axes.labelcolor": "#777777", "xtick.color": "#888888", "ytick.color": "#888888",
    "text.color": "#777777", "font.size": 8, "axes.grid": False, "axes.titlesize": 8,
    "axes.prop_cycle": cycler(color=["#5B4FD6", "#E8604C", "#F0A830", "#2BB59C"]),
    "legend.fontsize": 8, "figure.facecolor": "white",
})
_legend = matplotlib.axes.Axes.legend
def _above(self, *a, **k):
    k.setdefault("loc", "lower center"); k.setdefault("bbox_to_anchor", (0.5, 0.98))
    return _legend(self, *a, **k)
matplotlib.axes.Axes.legend = _above
'''

# (dialog, [(type, content, output, run)]) in store order: the last five are "Recent".
STUB = lambda title, line: [("note", f"# {title}\n\n{line}", "", False)]
DIALOGS: list[tuple[str, list]] = [
    ("fourier/gibbs", STUB("Gibbs phenomenon", "Why does the overshoot near a jump never go away?")),
    ("fourier/triangle-wave", STUB("Triangle wave", "Same recipe as the square wave, but the harmonics fall off as 1/k².")),
    ("fourier/square-wave", [
        ("note", "# Why only odd harmonics?", "", False),
        ("prompt", WAVE_Q, WAVE_A1, False),
        ("code", WAVE_CODE3, "", True),
        ("note", WAVE_NOTE4, "", False),
        ("prompt", WAVE_Q5, WAVE_A5, False),
        ("code", WAVE_CODE6, "", True),
    ]),
    ("attention/from-scratch", STUB("Attention from scratch", "Where do I start with Q and K?")),
    ("attention/rope", STUB("Rotary embeddings", "Rotate pairs of channels by a position-dependent angle.")),
    ("audio/mfcc", STUB("MFCCs", "Mel filterbank, log, then a DCT.")),
    ("audio/snake-activation", STUB("Snake activation", "x + sin²(αx)/α: a periodic bias for audio generators.")),
    ("papers/attention-is-all-you-need", STUB("Attention Is All You Need", "Reading notes.")),
    ("rl/policy-gradients", STUB("Policy gradients", "REINFORCE in twenty lines.")),
]


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("dir", type=Path, help="scratch directory (created if missing)")
    ap.add_argument("--kernel-url", default="http://localhost:5155")
    ap.add_argument("--force", action="store_true",
                    help="replace a notebook this script didn't write")
    a = ap.parse_args()
    root = a.dir.resolve()
    home_cfg = (Path.home() / ".config").resolve()
    if root == home_cfg or home_cfg in root.parents:
        sys.exit("refusing to write under ~/.config")
    data = root / "data"
    store, marker = data / "dialogs-kernel.json", data / ".demo-data"
    folder = data / "dialogs-kernel.d"
    if (store.exists() or folder.exists()) and not marker.exists() and not a.force:
        sys.exit(f"refusing to replace {folder}: this script didn't write it (--force to anyway)")
    data.mkdir(parents=True, exist_ok=True)
    (data / ".dandelion-store").write_text("")     # a data folder needs its marker
    (root / "papers").mkdir(exist_ok=True)
    os.environ["DANDELION_DATA"] = str(data)       # before importing dandelion

    # a targets.yaml whose `kernel` target points at the scratch kernel (the real one is untouched)
    repo = Path(__file__).resolve().parent.parent
    cfg = (repo / "targets.yaml").read_text()
    cfg = re.sub(r"http://localhost:5055", a.kernel_url.rstrip("/"), cfg)
    (root / "targets.yaml").write_text(cfg)

    sys.path.insert(0, str(repo))
    from dandelion.client import HttpKernelBackend
    from dandelion.targets import Target

    store.unlink(missing_ok=True)
    shutil.rmtree(folder, ignore_errors=True)
    marker.write_text("written by scripts/demo_data.py\n")
    be = HttpKernelBackend(Target(name="kernel", url=a.kernel_url, token="dummy", backend="kernel"))
    for dialog, cells in DIALOGS:
        if dialog == "fourier/square-wave":
            be._post("/exec", {"dialog": dialog, "code": STYLE})
        for typ, content, output, run in cells:
            m = be.add(dialog, content, typ, model=MODEL if typ == "prompt" else None,
                       ai_mode="learning" if typ == "prompt" else None)
            if typ == "prompt":
                be.update_output(dialog, m.id, output)       # pre-written answer, no AI call
            elif run:
                be.exec(dialog, m.id)                        # really executed on the kernel
                print(f"ran {dialog}: {m.output.strip()[:60]!r} rich={len(m.rich)}")
    print(f"wrote {folder} and {root / 'targets.yaml'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
