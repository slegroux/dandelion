"""Dandelion — a local-first notebook that teaches: the AI hints, you run the code.
Runs against your laptop or a remote H100, switchable at runtime."""
__version__ = "0.1.0"

# Former names: settings exported as PAPPUS_* or SIDEKICK_* (old launchers,
# other machines, shell profiles) keep working as their DANDELION_* equivalents.
# An explicit DANDELION_* value always wins, then PAPPUS_*, then SIDEKICK_*.
import os as _os

for _old in ("PAPPUS_", "SIDEKICK_"):
    for _k, _v in list(_os.environ.items()):
        if _k.startswith(_old):
            _os.environ.setdefault("DANDELION_" + _k[len(_old):], _v)
