"""Where your notebooks and everything else you create live — and how to move them.

All user data is local to the machine and never part of the git repo:

    ~/.config/dandelion/          (override with DANDELION_DATA, or the Settings
                                   "Data folder", which saves a pointer file
                                   ~/.config/dandelion/data-root)
        dialogs-<target>.d/              notebooks: one file per dialog (cells, outputs)
        dialogs-<target>.local.d/        dialogs marked "keep local only"
        dialogs-<target>.private         names of those dialogs
        recall.json                      spaced-repetition schedule
        libraries.json                   registered nbdev libraries
        blog/                            the Quarto blog project
        papers/                          reading library (DANDELION_PAPERS)
        tools.json, secrets.json         Ask-AI tool policy, API keys
                                         (these two always stay in ~/.config/dandelion)

A data folder other than ~/.config/dandelion must hold a `.dandelion-store` marker,
or it is refused and nothing is written there (an unmounted drive looks like an
empty folder). Only "Use this folder" in Settings creates the marker. While a
server runs, a `.dandelion-lease` there tells another machine sharing the folder
that it is open, and that machine goes read-only (see acquire_lease).

`backup()` packs that into one .tar.gz and `restore()` unpacks it on a new
install (`dandelion backup` / `dandelion restore`). API keys are left out of a
backup unless asked for, so an archive can be copied around safely.

`migrate_repo_data()` moves an old in-repo store (`<repo>/data`, which the
justfile used to point PAPPUS_DATA at and `just sync` pushed to GitHub) into
the local home once, never overwriting anything already there.
"""
from __future__ import annotations

import contextlib
import json
import os
import re
import shutil
import socket
import tarfile
import threading
import time
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath

REPO_DATA = Path(__file__).resolve().parent.parent / "data"

# rolling save-backups and half-written temp files are not worth carrying over
_SKIP_SUFFIXES = (".bak", ".tmp", ".restore-tmp", ".tar.gz")   # .tar.gz: older backups
MARKER = ".dandelion-store"      # says "this folder is a data store"; not part of the data
LEASE = ".dandelion-lease"       # which machine has the folder open (see acquire_lease)
# data-root, machine-id, unsaved-*: this machine's pointer, identity and recovered edits
_SKIP_NAMES = {".DS_Store", ".serve.pid", MARKER, LEASE, "data-root", "machine-id"}
_SKIP_DIRS = {".git", "__pycache__", ".ipynb_checkpoints", ".quarto"}


def _skipped(name: str) -> bool:
    """Files that are never backed up, restored or migrated."""
    return name in _SKIP_NAMES or name.endswith(_SKIP_SUFFIXES) or name.startswith("unsaved-")


def config_dir() -> Path:
    """Where keys, tool policy and the data-folder pointer live, whatever the data root."""
    return Path.home() / ".config" / "dandelion"


def _pointer_file() -> Path:
    return config_dir() / "data-root"


def _samefile(a: Path, b: Path) -> bool:
    """Same file by device and inode, so case variants and links of a path match
    (APFS is case-insensitive and resolve() keeps the case you typed)."""
    try:
        return os.path.samefile(a, b)
    except OSError:
        return False


def _dangerous(p: Path) -> bool:
    """Folders that must never be a data folder: /, $HOME, anything above the config
    dir (backup would tar it), and the repo (where `just sync` once pushed data)."""
    repo = REPO_DATA.parent
    if p in config_dir().parents or any(_samefile(p, b) for b in (Path(p.anchor or "/"), Path.home(), *config_dir().parents)):
        return True
    return any(_samefile(q, repo) for q in (p, *p.parents))


def _pointer_state() -> tuple[Path | None, str | None]:
    """(folder, None) for a good pointer file, (None, problem) for one that is
    there but not exactly one usable absolute path, (None, None) when absent."""
    try:
        text = _pointer_file().read_text()
    except FileNotFoundError:
        return None, None
    except OSError as e:
        return None, f"unreadable: {e.strerror or e}"
    lines = [x.strip() for x in text.splitlines() if x.strip()]
    if len(lines) != 1 or "\0" in lines[0]:
        return None, "it must hold exactly one path"
    p = Path(lines[0]).expanduser()
    if not p.is_absolute():
        return None, "the path is not absolute"
    if _dangerous(p):
        return None, f"{p} cannot be a data folder"
    return p, None


def read_pointer() -> Path | None:
    """The folder a good pointer file names, else None."""
    return _pointer_state()[0]


def _resolve() -> tuple[Path, str, str | None]:
    """(root, source, problem) as the environment and pointer file say right now."""
    env = os.environ.get("DANDELION_DATA")
    if env:
        return Path(env).expanduser(), "env", None
    p, bad = _pointer_state()
    if p:
        return p, "pointer", None
    if bad:
        return config_dir(), "pointer", f"The data-root setting is invalid ({_pointer_file()}): {bad}"
    return config_dir(), "default", None


# The root and whether it is refused are decided once per process (freeze_root, at
# serve start) and stay that way until restart: the dialogs in memory were loaded
# from that root, so a pointer change or a mount appearing later must not redirect
# saves. Without it (tests, one-shot CLI commands) everything is looked up afresh.
_FROZEN: dict | None = None


def data_root() -> Path:
    return _FROZEN["root"] if _FROZEN else _resolve()[0]


def data_root_source() -> str:
    """Why data_root() is what it is: 'env' | 'pointer' | 'default'."""
    return _FROZEN["source"] if _FROZEN else _resolve()[1]


def pending_root() -> Path | None:
    """The folder a restart would use, when that differs from the one in use."""
    if not _FROZEN:
        return None
    root, _, bad = _resolve()
    return None if bad or root == _FROZEN["root"] else root


class StoreRefused(OSError):
    """The data folder has no marker; nothing may be written to it. An OSError, so
    the best-effort savers that already swallow disk errors swallow this too."""


def _is_default(root: Path) -> bool:
    """The default root is exempt from the marker, but only as a real directory: a
    link (adopt_legacy_root keeps a user's link into pCloud) may point at an empty mount."""
    cfg = config_dir()
    return not cfg.is_symlink() and _same(root, cfg)


def _marker_problem(root: Path) -> str | None:
    """None when the marker is there, 'missing', or why the folder can't be read."""
    try:
        os.stat(root / MARKER)
        return None
    except (FileNotFoundError, NotADirectoryError):
        return "missing"
    except OSError as e:
        return f"Cannot access {root} ({e.strerror or e})."


def _refusal(root: Path, source: str, bad: str | None) -> str | None:
    if bad:
        return bad
    if _is_default(root):
        return None
    m = _marker_problem(root)
    if m is None:
        return None
    if m != "missing":
        return m
    base = (f"{root} has no {MARKER} marker; the folder may be an unmounted drive "
            "(e.g. pCloud not running). Nothing is saved there. ")
    if source == "env":
        return base + (f"Start the drive and restart Dandelion; if this is the right folder, run "
                       f"touch '{root}/{MARKER}' or unset DANDELION_DATA.")
    if config_dir().is_symlink() and _same(root, config_dir()):
        return base + (f"{config_dir()} is a link to another place. Start the drive and restart "
                       f"Dandelion, or run touch '{root}/{MARKER}' if it is the right folder.")
    return base + ("Start your cloud drive, then restart Dandelion; or open Settings to pick "
                   "another folder or use this one.")


def _root_refusal() -> str | None:
    if _FROZEN:
        return _FROZEN["refusal"] or _refusal(_FROZEN["root"], _FROZEN["source"], None)
    return _refusal(*_resolve())


def store_refusal() -> str | None:
    """Why the data root is refused (a message for the UI), or None when it may be
    used. Once frozen, a refusal stays until restart; an accepted root is still
    re-checked on every call, so a mount that goes away stops the writes. A root
    another machine has open (see acquire_lease) is refused as in use."""
    return _root_refusal() or _in_use_refusal()


def check_writable(path: Path | str | None = None) -> None:
    """Call before writing under the data root. Raises StoreRefused while the root
    is refused (StoreInUse while another machine has it open); `path` limits that
    to paths inside the root (papers may live elsewhere)."""
    msg, cls = _root_refusal(), StoreRefused
    if not msg:
        msg, cls = _in_use_refusal(), StoreInUse
    if msg and (path is None or _same(Path(path), data_root())
                or data_root() in Path(path).parents):
        raise cls(msg)


_warned: set[str] = set()


def warn_refused(e: Exception) -> None:
    """Say on stderr, once per message, that a save was refused."""
    import sys
    if str(e) not in _warned:
        _warned.add(str(e))
        print(f"[dandelion] WARNING: not saving: {e}", file=sys.stderr)


def make_dirs(d: Path) -> None:
    """mkdir -p for a folder under the data root without ever creating a non-default
    root itself (a mount that vanished after check_writable must fail, not be recreated)."""
    root = data_root()
    if _is_default(root) or (d != root and root not in d.parents):
        d.mkdir(parents=True, exist_ok=True)
        return
    if d == root:
        return                       # a non-default root must already be there
    cur = root
    for part in d.relative_to(root).parts:
        cur = cur / part
        cur.mkdir(exist_ok=True)


def claim_default_root() -> None:
    """Write the marker into the default root so it is never refused. Silent and
    best-effort. Other roots get theirs from use_folder, or from freeze_root when
    they already hold a store."""
    root = _resolve()[0]
    if not _is_default(root) or (root / MARKER).is_file():
        return
    with contextlib.suppress(OSError):
        root.mkdir(parents=True, exist_ok=True)
        (root / MARKER).write_text("")


def _has_store(root: Path) -> bool:
    """A dialogs-<target>.json (or a dialogs-<target>.d folder) that reads as a store
    with at least one dialog: an unmounted mountpoint is empty, and a stray empty or
    local-only file proves nothing."""
    from .client import _parse_store
    try:
        return any(f.is_file() and not f.name.endswith(".local.json") and _parse_store(f)
                   for f in root.glob("dialogs-*.json")) \
            or any(_parse_store(f) for d in root.glob("dialogs-*.d")
                   if d.is_dir() and not d.name.endswith(".local.d")
                   for f in d.glob("*~*.json"))
    except OSError:
        return False


def freeze_root() -> None:
    """Decide the data root and whether it is refused, once, for this process (called
    at serve start and when the backend is built). A root that already holds a
    dialogs-*.json is a real store, since an unmounted drive is empty: it is
    accepted and marked. Anything else unmarked stays refused until restart."""
    import sys
    global _FROZEN
    if _FROZEN:
        return
    claim_default_root()
    root, source, bad = _resolve()
    msg = _refusal(root, source, bad)
    # pointer roots get their marker from use_folder, never from this rule
    if msg and not bad and _marker_problem(root) == "missing" \
            and (source == "env" or (config_dir().is_symlink() and _same(root, config_dir()))) \
            and _has_store(root):
        with contextlib.suppress(OSError):
            (root / MARKER).write_text("")
            print(f"[dandelion] marked existing store at {root}", file=sys.stderr)
            msg = None
    _FROZEN = {"root": root, "source": source, "refusal": msg}
    if msg:
        print(f"[dandelion] WARNING: {msg}", file=sys.stderr)


def unfreeze_root() -> None:
    """Back to looking the root up on every call (test fixture)."""
    global _FROZEN
    _FROZEN = None
    _warned.clear()
    _reset_lease()


def use_folder(path: str, confirm: bool = False) -> tuple[bool, str]:
    """Settings "Use this folder": make `path` the data folder from the next start.
    Creates only the folder itself (its parent must exist), writes the marker, and
    moves or copies nothing. Unless it already carries the marker, `confirm` is
    needed when the folder is empty (an unmounted drive looks empty), holds other
    files, or is the root being refused now. Returns (ok, message)."""
    p = Path(path.strip()).expanduser() if path.strip() else None
    if p is None or not p.is_absolute() or "\0" in str(p):
        return False, "Give an absolute path (it may start with ~)."
    if _dangerous(p):
        return False, f"{p} cannot be a data folder (pick a folder of its own)."
    drive = ("Is your cloud drive running? An unmounted drive looks like an empty folder. "
             "If this really is the folder you mean, tick the confirmation and press the "
             "button again.")
    old = data_root()
    try:
        if p.exists() and not p.is_dir():
            return False, f"{p} is not a folder."
        if not p.parent.is_dir():
            return False, f"{p.parent} does not exist. {drive.split(' If ')[0]}"
        marked = (p / MARKER).is_file()
        if not marked and not confirm:
            if p.is_dir() and any(p.iterdir()):
                return False, (f"{p} already holds files but no {MARKER} marker. If it is "
                               "the folder you mean, tick the confirmation and press the "
                               "button again.")
            if p.is_dir() or not any(p.parent.iterdir()) \
                    or (_same(p, data_root()) and store_refusal()):
                return False, drive
        p.mkdir(exist_ok=True)
        (p / MARKER).write_text("")
        _write_pointer(p)
    except OSError as e:
        return False, f"Could not use {p}: {e.strerror or e}"
    return True, (f"Saved. Dandelion will use {p} after you restart it. Nothing was moved or "
                  "copied: to bring your dialogs along, run "
                  f"DANDELION_DATA='{old}' uv run python -m dandelion.cli backup now (a plain "
                  "backup would archive the new folder), then restore it after the restart.")


def _write_pointer(p: Path) -> None:
    f = _pointer_file()
    f.parent.mkdir(parents=True, exist_ok=True)
    tmp = f.with_name(f"{f.name}.tmp")
    tmp.write_text(str(p) + "\n")
    tmp.replace(f)


def clear_pointer() -> tuple[bool, str]:
    """Settings "Back to the default": forget the pointer; the data stays where it is."""
    try:
        _pointer_file().unlink(missing_ok=True)
    except OSError as e:
        return False, f"Could not clear the setting: {e.strerror or e}"
    return True, (f"Saved. Dandelion will use {config_dir()} after you restart it. "
                  "Nothing was moved.")


# The data root's names before the renames, newest first.
LEGACY_ROOT_NAMES = ("pappus", "solveit-" + "sidekick")


class MigrationNotice(Exception):
    """adopt_legacy_root left something for the user to sort out; the message
    says what. The CLI prints it and carries on."""


class LegacyAppRunning(MigrationNotice):
    def __init__(self, pid: int, old: Path):
        self.pid = pid
        super().__init__(f"An older Pappus server (pid {pid}) is still running; stop it "
                         "(just stop) and start again to move your data to "
                         f"~/.config/dandelion. If it isn't running, delete {old / '.serve.pid'}")


def _legacy_roots(cfg: Path) -> list[Path]:
    """Every spelling of the old roots a path or cache key may carry: under the
    literal ~/.config and under the resolved one (cache keys hash resolved paths)."""
    return list(dict.fromkeys(base / n for base in (cfg, cfg.resolve())
                              for n in LEGACY_ROOT_NAMES))


def legacy_file(name: str) -> Path | None:
    """`name` (secrets.json, tools.json) under the newest old root that has it.
    Lets keys and tool policy follow where the data move didn't run (an explicit
    data dir, a kernel-only machine)."""
    cfg = Path.home() / ".config"
    return next((cfg / n / name for n in LEGACY_ROOT_NAMES if (cfg / n / name).is_file()), None)


def _explicit_elsewhere(cfg: Path) -> bool:
    """DANDELION_DATA (PAPPUS_/SIDEKICK_DATA arrive aliased to it) names a dir
    other than the new or an old default root: the user chose it, leave it be."""
    env = os.environ.get("DANDELION_DATA")
    if not env:
        return False
    p = Path(env).expanduser()
    defaults = [*_legacy_roots(cfg), cfg / "dandelion", cfg.resolve() / "dandelion"]
    return not any(p == d or _same(p, d) for d in defaults)


def _is_rename_link(link: Path, cfg: Path) -> bool:
    """A link an earlier rename left (to another old root or the new one), as
    opposed to one the user made (into pCloud, onto an external drive…)."""
    t = Path(os.readlink(link))
    t = t if t.is_absolute() else cfg / t
    return t.name in (*LEGACY_ROOT_NAMES, "dandelion") and _same(t.parent, cfg)


def adopt_legacy_root(home: Path | None = None) -> Path | None:
    """First start after a rename: move ~/.config/<old name> to ~/.config/dandelion
    and leave a symlink at the old path, so old scripts and backups still find
    the data. The newest old root that is a real directory moves (an older one is
    normally a link an earlier rename left). If it is instead a link the user
    made, ~/.config/dandelion becomes a link to the same place and nothing moves.
    Skipped when DANDELION_DATA names a dir other than a default root; never
    overwrites. Converted papers are re-keyed to their new paths. Called by the
    CLI, never from data_root(), so a path lookup can't move data. Returns the
    new root when it moved or linked something. Raises MigrationNotice when it
    left something for the user: an old server still running, data in both
    roots, or a link it could not make."""
    cfg = (home or Path.home()) / ".config"
    if _explicit_elsewhere(cfg):
        return None
    new = cfg / "dandelion"
    olds = [cfg / n for n in LEGACY_ROOT_NAMES]
    if new.exists() or new.is_symlink():
        for old in olds:
            if old.is_dir() and not old.is_symlink() and not _same(old, new) \
                    and any(old.iterdir()):
                raise MigrationNotice(f"both {old} and {new} hold data; using {new}. "
                                      f"Nothing was moved: copy what you need from {old}, "
                                      "then remove it")
        return None
    for old in olds:
        if old.is_symlink():
            if _is_rename_link(old, cfg):
                continue
            new.symlink_to(os.readlink(old), target_is_directory=True)
            return new
        if old.is_dir():
            break
    else:
        return None
    pid = running_server_pid(old)
    if pid:
        raise LegacyAppRunning(pid, old)
    old_real = old.resolve()
    old.rename(new)
    try:
        old.symlink_to(new, target_is_directory=True)
        link_error = None
    except OSError as e:
        link_error = e
    # the move is done; a paper that misses its cache just re-converts, and the
    # CLI re-runs the re-key (rekey_papers) on every start
    with contextlib.suppress(Exception):
        _rekey_moved_papers(new, [old_real, *_legacy_roots(cfg)])
    if link_error:
        raise MigrationNotice(f"moved {old} to {new}, but could not leave a link at "
                              f"{old}: {link_error}")
    return new


def rekey_papers(home: Path | None = None) -> int:
    """Re-key the current root's papers against every old root. Cheap and
    idempotent, so the CLI runs it on every start: it finishes a move whose
    re-keying was cut short."""
    if store_refusal():
        return 0
    return _rekey_moved_papers(data_root(), _legacy_roots((home or Path.home()) / ".config"))


def _rewrite_sources(src: Path, repoint) -> bool:
    """Apply `repoint(source) -> new source or None` to each dialog's entry in a
    papers sources.json, written atomically. Returns whether anything changed."""
    import json

    from .paper import _read_json_dict
    recs = _read_json_dict(src)
    changed = False
    for dialog, rec in (recs or {}).items():
        path = rec.get("source") if isinstance(rec, dict) else None
        new = repoint(path) if isinstance(path, str) and path else None
        if new and new != path:
            recs[dialog] = {**rec, "source": new}
            changed = True
    if changed:
        tmp = src.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(recs, indent=1))
        tmp.replace(src)
    return changed


def _rekey_moved_papers(root: Path, old_roots: list[Path]) -> int:
    """After the data root moved, give each converted upload a cache entry under
    its new path-based key (paper._md_key hashes the absolute path), so papers
    reopen without a slow marker re-conversion, and re-point sources.json entries
    that name a file under an old root. Every old root is tried, since a paper
    converted before an earlier rename still carries that root's key. The papers
    dir is DANDELION_PAPERS when that lies under the root, else root/papers.
    Never overwrites; one unreadable upload skips only itself. Returns how many
    conversions were re-keyed."""
    from .paper import _cache_dir, _md_key, _path_key
    papers, rel = root / "papers", "papers"
    try:
        cache = _cache_dir()
        rel = cache.resolve().relative_to(root.resolve()).as_posix()
        papers = cache
    except (OSError, ValueError):                # papers live outside the root
        pass
    roots = list(dict.fromkeys(str(r) for r in old_roots))
    up = papers / "uploads"
    n = 0
    for pdf in sorted(up.glob("*.pdf")) if up.is_dir() else []:
        try:
            new_md = papers / f"{_md_key(pdf)}.md"
            if new_md.exists():
                continue
            stamp = pdf.stat().st_mtime_ns
            for r in roots:
                old_md = papers / f"{_path_key(f'{r}/{rel}/uploads/{pdf.name}', stamp)}.md"
                if old_md.is_file():
                    shutil.copy2(old_md, new_md)
                    n += 1
                    break
        except OSError:                          # dangling or unreadable upload
            continue

    def repoint(path: str) -> str | None:
        for r in roots:
            if path.startswith(r + os.sep):
                moved = root / path[len(r) + 1:]
                return str(moved) if moved.exists() else None
        return None
    try:
        _rewrite_sources(papers / "sources.json", repoint)
    except (OSError, ValueError):
        pass
    return n


def _config_files(write: bool = False) -> dict[str, Path]:
    """Config that lives at its own (env-overridable) path, keyed by archive name.
    `write` gives where it is saved, not a legacy copy it may still be read from."""
    from .secrets_store import secrets_path
    from .tools_config import tools_path
    return {"config/tools.json": tools_path(write), "config/secrets.json": secrets_path(write)}


def _same(a: Path, b: Path) -> bool:
    try:
        return a.resolve() == b.resolve()
    except OSError:
        return False


# ---- one-time move out of the repo ------------------------------------------
def migrate_repo_data(old: Path | None = None) -> list[str]:
    """Move an old in-repo data dir into data_root(). Non-destructive: an entry
    that already exists at the destination is left in both places (reported by
    the caller as skipped). Papers go through paper.migrate_legacy_cache so
    figure links survive and conversions are re-keyed. Returns what moved."""
    from . import paper
    old = old or REPO_DATA
    new = data_root()
    if not old.is_dir() or _same(old, new) or store_refusal():
        return []
    new.mkdir(parents=True, exist_ok=True)
    moved = [f"papers/{m}" for m in paper.migrate_legacy_cache(old / "papers")]
    for src in sorted(old.iterdir()):
        if src.name == "papers" or _skipped(src.name):
            continue
        dst = new / src.name
        if dst.exists():
            continue
        shutil.move(str(src), dst)
        moved.append(src.name)
    return moved


def leftovers(old: Path | None = None) -> list[str]:
    """Entries still in the old in-repo dir after migration (name clashes)."""
    old = old or REPO_DATA
    if not old.is_dir() or _same(old, data_root()):
        return []
    out = []
    for p in sorted(old.rglob("*")):
        if p.is_file() and not _skipped(p.name) \
                and p.name != "sources.json.migrated":
            out.append(str(p.relative_to(old)))
    return out


# ---- is the app running? -------------------------------------------------------
def _pidfile() -> Path:
    return data_root() / ".serve.pid"


def serve_refusal(pid: int) -> str:
    """Why a second `dandelion serve` on this data folder is refused."""
    return (f"refusing to start: another Dandelion server (pid {pid}) already serves "
            f"{data_root()}. "
            "Two servers on one data folder overwrite each other's dialogs. "
            f"Stop the other one with kill {pid} (just stop, if it's on the default ports), "
            "or run this one on its own copy of the data, e.g. "
            "mkdir -p /tmp/dandelion-test && touch /tmp/dandelion-test/.dandelion-store && "
            "DANDELION_DATA=/tmp/dandelion-test DANDELION_PORT=8002 "
            "uv run python -m dandelion.cli serve (the marker lets a new folder save). "
            f"If no server is running, delete {_pidfile()}")


def claim_serving() -> None:
    """Check that no other live server holds this data root and record this
    `dandelion serve` process in .serve.pid (restore refuses to run under it).
    Raises AppRunning when another server is live, or when the record can't be
    written: an unrecorded server would leave the folder open to a second one."""
    import atexit
    f, me = _pidfile(), os.getpid()
    rec = f"{me} {_machine()}"         # the pid means something only on its own machine
    if store_refusal():
        return                         # a refused folder gets no record (and nothing else)
    other = running_server_pid()
    if other:
        raise AppRunning(serve_refusal(other))
    tmp = f.with_name(f"{f.name}.{me}.tmp")          # unique, and skipped by backups
    try:
        make_dirs(f.parent)
        tmp.write_text(rec)
        try:
            try:
                os.link(tmp, f)                  # creates f with its content, or fails
            except FileExistsError:              # stale, or a server that just won the race
                other = running_server_pid()
                if other:
                    raise AppRunning(serve_refusal(other)) from None
                tmp.replace(f)
            except OSError:                      # no hard links here (some FUSE mounts)
                fd = os.open(f, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
                with os.fdopen(fd, "w") as fh:
                    fh.write(rec)
        finally:
            tmp.unlink(missing_ok=True)
    except OSError as e:
        raise AppRunning(f"not starting: could not record this server in {f} ({e.strerror or e}). "
                         "Without that record a second server could start on this folder. "
                         "Fix the folder's permissions, or use another DANDELION_DATA") from e

    def _clear():
        try:
            if f.read_text().split()[:1] == [str(me)]:
                f.unlink()
        except OSError:
            pass
    atexit.register(_clear)
    acquire_lease()                    # .serve.pid covers this machine, the lease the others


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


_SERVER_CMD = re.compile(r"\b(dandelion|pappus|sidekick)(\.cli)?\s+serve\b", re.IGNORECASE)


def _looks_like_dandelion(pid: int) -> bool:
    """Whether `pid` runs `dandelion serve` (also as pappus or sidekick, its names
    before the renames), so a stale pid file whose pid the OS reused for an
    unrelated process doesn't count as a server. If ps can't answer (missing,
    timing out), assume it does."""
    import subprocess
    try:
        r = subprocess.run(["ps", "-p", str(pid), "-o", "command="], check=False,
                           capture_output=True, text=True, timeout=5)
    except (OSError, subprocess.SubprocessError):
        return True
    if r.returncode != 0 or not r.stdout.strip():
        return _pid_alive(pid)      # ps failed: gone since the check, or can't tell
    return bool(_SERVER_CMD.search(r.stdout))


def running_server_pid(root: Path | None = None) -> int | None:
    """PID of a live `dandelion serve` using this data root (or `root`), else None.
    A record from another machine (the folder is synced) says nothing about this
    one's processes: that is the lease's job. A bare pid is an older record, from here."""
    try:
        pid, *machine = ((root / ".serve.pid") if root else _pidfile()).read_text().split()
        pid = int(pid)
    except (OSError, ValueError):
        return None
    if machine and machine[0] != _machine():
        return None
    return pid if pid != os.getpid() and _pid_alive(pid) and _looks_like_dandelion(pid) else None


class AppRunning(RuntimeError):
    pass


# ---- is it open on another machine? ------------------------------------------
# .serve.pid only sees this machine. A data folder synced between Macs (pCloud)
# also carries a lease: the serving process rewrites it every LEASE_HEARTBEAT_S,
# and a server on another machine that finds it alive opens the folder read-only.
# A sync folder has no atomic compare-and-swap (O_EXCL is not exclusive on pCloud),
# each machine sees the other's writes only after a sync, and a reader can see a
# file briefly missing while it is replaced. So this is a warning that makes the
# second machine stop saving, not a lock; the compare-then-replace windows below
# are kept to one read and one rename, and every doubt fails closed (read-only):
# - machines are told apart by their hardware id, not the hostname (two Macs can
#   share one, and a router can rename a Mac);
# - another machine's lease is judged stale only when its heartbeat has not
#   changed for LEASE_STALE_S on this Mac's own clock (clocks may disagree);
# - a Mac that slept or stalled past its own lease (no beat for most of
#   LEASE_STALE_S, by its own clock) stops saving at once: the other Mac may have
#   taken over meanwhile. It waits LEASE_GRACE_S for the sync before 'Check again'.
#   Not on the default root (~/.config/dandelion as a real folder): it is never
#   synced, so no other Mac can have taken it.
def _heartbeat_for(beat: float, stale: float) -> float:
    """A beat often enough that a live lease never looks stale or lapsed."""
    if beat < stale / 2:
        return beat
    import sys
    print(f"[dandelion] WARNING: DANDELION_LEASE_HEARTBEAT_S={beat:g} must be under half of "
          f"DANDELION_LEASE_STALE_S={stale:g}; using {stale / 4:g}", file=sys.stderr)
    return stale / 4


LEASE_STALE_S = float(os.environ.get("DANDELION_LEASE_STALE_S", "300"))
LEASE_HEARTBEAT_S = _heartbeat_for(float(os.environ.get("DANDELION_LEASE_HEARTBEAT_S", "60")),
                                   LEASE_STALE_S)
LEASE_GRACE_S = float(os.environ.get("DANDELION_LEASE_GRACE_S", "45"))
LEASE_UNSEEN_S = 3600          # at start, a lease younger than this by its stamp counts as live
LEASE_RETRIES = 3              # a missing or unreadable lease is read again this many times,
LEASE_RETRY_S = float(os.environ.get("DANDELION_LEASE_RETRY_S", "1"))   # this far apart
_FENCE = 0.8                   # our own hold lapses after _FENCE * LEASE_STALE_S without a beat
_UNREADABLE = object()         # read_lease(): the file is there but can't be read
_LEASE_LOCK = threading.RLock()
# mine: the record this process holds (beat_at: when it last wrote it, this
# Mac's clock); last: the record it held before a lapse; other: the record of
# the machine that has the folder open (this process is read-only); lapsed:
# when this Mac's hold lapsed (monotonic); seen: (record key, monotonic first
# seen) of another machine's lease; notice: a take-over to tell the user about;
# own_retake: set while take_lease reloads for our own unchanged record.
# Read-only is decided only at start (acquire_lease), by a heartbeat (_tick),
# by a save noticing a lapse, and by 'Check again' / 'Take over' (take_lease).
_LEASE: dict = {"mine": None, "beat_at": 0.0, "last": None, "other": None, "lapsed": None,
                "seen": None, "notice": None, "stop": None, "exit_hook": False,
                "own_retake": False, "fenced": True}
_ID: dict = {}


class StoreInUse(StoreRefused):
    """Another machine has the data folder open, or this Mac's hold lapsed; this
    process saves nothing."""


def _host() -> str:
    return socket.gethostname()


def _run(*cmd: str) -> str:
    import subprocess
    try:
        r = subprocess.run(cmd, check=False, capture_output=True, text=True, timeout=5)
    except (OSError, subprocess.SubprocessError):
        return ""
    return r.stdout if r.returncode == 0 else ""


def local_dir() -> Path:
    """Where this machine's own files go (its machine-id, edits kept by a reload):
    config_dir(), unless that is a link or lies outside the home folder (an old
    ~/.config/pappus link into pCloud), then ~/Library/Application Support/dandelion,
    then the temp folder. Never a synced folder; never backed up."""
    import tempfile
    cfg, home = config_dir(), Path.home()
    try:
        if not cfg.is_symlink() and cfg.resolve().is_relative_to(home.resolve()):
            return cfg
    except OSError:
        pass
    for d in (home / "Library" / "Application Support" / "dandelion",
              Path(tempfile.gettempdir()) / f"dandelion-{os.getuid()}"):
        with contextlib.suppress(OSError):
            d.mkdir(parents=True, exist_ok=True)
            if not d.is_symlink():
                return d
    return Path(tempfile.gettempdir())


def _local_machine_id() -> str:
    """A random id kept in local_dir()/machine-id (never backed up or restored, so a
    restored Mac doesn't pose as the old one, and never synced). If it can't be
    stored, the hostname plus a random id for this process: then this process
    looks like another machine to every other, which fails closed."""
    import uuid
    f = local_dir() / "machine-id"
    with contextlib.suppress(OSError):
        if v := f.read_text().strip():
            return v
    v = str(uuid.uuid4())
    try:
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(v + "\n")
    except OSError:
        return f"{_host()}-{v}"
    return v


def _machine() -> str:
    """This machine's id: the Mac's IOPlatformUUID, else _local_machine_id()."""
    if "machine" not in _ID:
        m = re.search(r'"IOPlatformUUID" = "([^"]+)"',
                      _run("ioreg", "-rd1", "-c", "IOPlatformExpertDevice"))
        _ID["machine"] = m.group(1) if m else _local_machine_id()
    return _ID["machine"]


def _machine_name() -> str:
    """The name people know this Mac by (System Settings > Sharing), else the hostname."""
    if "name" not in _ID:
        _ID["name"] = _run("scutil", "--get", "ComputerName").strip() or _host()
    return _ID["name"]


def _lease_file() -> Path:
    return data_root() / LEASE


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _read_lease_once():
    try:
        text = _lease_file().read_text()
    except FileNotFoundError:
        return None
    except OSError:
        return _UNREADABLE
    try:
        v = json.loads(text)
    except ValueError:
        return _UNREADABLE
    ok = isinstance(v, dict) and all(isinstance(v.get(k), str) for k in ("machine", "heartbeat"))
    return v if ok else _UNREADABLE


def read_lease():
    """The lease record in the data root, None when there is none, or _UNREADABLE.
    A synced file can look missing or torn for a moment while another machine
    replaces it, so either is read again a few times (not on the default root,
    which is never synced, when the lease is simply missing)."""
    rec = _read_lease_once()
    for _ in range(LEASE_RETRIES):
        if isinstance(rec, dict) or (rec is None and _is_default(data_root())):
            break
        time.sleep(LEASE_RETRY_S)
        rec = _read_lease_once()
    return rec


def _stamp_age(rec: dict) -> float:
    """Seconds since the record's heartbeat by its own stamp (another clock)."""
    try:
        return time.time() - datetime.fromisoformat(rec["heartbeat"]).timestamp()
    except (KeyError, TypeError, ValueError):
        return float("inf")


def _key(rec: dict) -> tuple:
    return rec.get("machine"), rec.get("pid"), rec.get("since")


def _is_mine(rec, record: dict | None) -> bool:
    return isinstance(rec, dict) and bool(record) and _key(rec) == _key(record)


def _observed_stale(rec: dict) -> bool:
    """Whether another machine's lease has not changed for LEASE_STALE_S, as this
    Mac has watched it (its stamps may come from a clock that is off)."""
    key, now = (*_key(rec), rec.get("heartbeat")), time.monotonic()
    seen = _LEASE["seen"]
    if not seen or seen[0] != key:
        _LEASE["seen"] = (key, now)
        return False
    return now - seen[1] >= LEASE_STALE_S


def _name(rec: dict) -> str:
    return str(rec.get("name") or rec.get("host") or "another machine")


def _seen_ago(rec: dict) -> str:
    """'last seen n min ago' by the record's own stamp, which is another clock."""
    age = _stamp_age(rec)
    if age == float("inf"):
        return "last seen long ago"
    if age < -60:
        return "dated in the future: its clock is ahead"
    return f"last seen {max(0, int(age // 60))} min ago"


def _reload_parent() -> bool:
    """This process ran `dandelion serve` with auto-reload; its worker serves."""
    return os.environ.get("_DANDELION_RELOAD_WORKER") == str(os.getpid())


def _judge(rec, start: bool) -> tuple[bool, str | None]:
    """Whether the lease just read may be taken, and the notice to show once it is
    (taking another machine's lease). When not, records who holds it (this process
    is then read-only). Caller holds _LEASE_LOCK."""
    if rec is None:
        return True, None
    if rec is _UNREADABLE:
        _LEASE["other"] = {"name": "an unknown machine (its lease file can't be read)"}
        return False, None
    if rec["machine"] == _machine():
        pid = rec.get("pid")
        if pid == os.getpid() or _is_mine(rec, _LEASE["last"]) \
                or str(pid) == os.environ.get("_DANDELION_RELOAD_WORKER") \
                or not isinstance(pid, int) or not _pid_alive(pid):
            return True, None            # ours, our reload parent's, or a run that ended
        _LEASE["other"] = rec            # another live process here: fail closed
        return False, None
    if _observed_stale(rec) or (start and _stamp_age(rec) >= LEASE_UNSEEN_S):
        return True, f"Took over the data folder from {_name(rec)} ({_seen_ago(rec)})."
    _LEASE["other"] = rec
    return False, None


def _write_lease(rec: dict) -> None:
    f = _lease_file()
    tmp = f.with_name(f"{f.name}.{os.getpid()}.tmp")       # skipped by backups
    try:
        tmp.write_text(json.dumps(rec))
        os.replace(tmp, f)                                  # atomic
    finally:
        tmp.unlink(missing_ok=True)


def _when(stamp) -> str:
    try:
        return datetime.fromisoformat(stamp).astimezone().strftime("%Y-%m-%d %H:%M")
    except (TypeError, ValueError):
        return "an unknown time"


def _lapsing() -> bool:
    return bool(_LEASE["mine"]) and _LEASE["fenced"] \
        and time.time() - _LEASE["beat_at"] >= _FENCE * LEASE_STALE_S


def _lapse() -> None:
    """Stop saving and beating: this Mac can't be sure it still holds the lease.
    Caller holds _LEASE_LOCK."""
    import sys
    if _LEASE["stop"]:
        _LEASE["stop"].set()
    _LEASE.update(last=_LEASE["mine"], mine=None, stop=None, lapsed=time.monotonic())
    print(f"[dandelion] WARNING: {lease_banner()}", file=sys.stderr)


def _check_lapse() -> bool:
    """Whether this Mac's hold has lapsed: no beat written for most of
    LEASE_STALE_S by this Mac's own clock (asleep, stalled, or the folder gone),
    so another Mac may have taken the lease. Then stop saving and beating."""
    if not _lapsing():                 # the common case takes no lock (saves call this)
        return _LEASE["lapsed"] is not None
    with _LEASE_LOCK:
        if _lapsing():
            _lapse()
        return _LEASE["lapsed"] is not None


def _in_use_refusal() -> str | None:
    if _check_lapse():
        return ("This Mac's hold on the data folder lapsed. Dandelion is read-only here; "
                "nothing is saved until you press Check again.")
    rec = _LEASE["other"]
    if not rec:
        return None
    since = f" since {_when(rec.get('since'))}" if "machine" in rec else ""
    return f"In use by {_name(rec)}{since}. Dandelion is read-only here; nothing is saved."


def lease_banner() -> str | None:
    """The page banner while this process is read-only because of the lease."""
    if _LEASE["lapsed"] is not None:
        return (f"This Mac's hold on the data folder lapsed (no heartbeat for over "
                f"{int(_FENCE * LEASE_STALE_S // 60)} min, e.g. after sleep), so another Mac "
                "may have taken over. Dandelion is read-only here.")
    rec = _LEASE["other"]
    if not rec:
        return None
    if "machine" not in rec:
        return f"In use by {_name(rec)}. Dandelion is read-only here."
    return (f"In use by {_name(rec)} since {_when(rec.get('since'))} ({_seen_ago(rec)}). "
            "Dandelion is read-only here.")


def lease_takeover_warning() -> str:
    rec = _LEASE["other"] or {}
    host = _name(rec) if "machine" in rec else "the other machine"
    return (f"Take over only if {host} is not running Dandelion on this folder; "
            "unsynced edits there may be lost.")


def lease_notice() -> str | None:
    """'Took over from …', when this process took a lease another machine left."""
    return _LEASE["notice"]


def lease_status() -> str | None:
    """Who has the data folder open, for Settings."""
    if banner := lease_banner():
        return banner
    mine = _LEASE["mine"]
    if not mine:
        return None
    if not _LEASE["fenced"]:
        return f"Open on this machine ({mine['name']}) since {_when(mine['since'])}."
    return (f"Open on this machine ({mine['name']}) since {_when(mine['since'])}; another "
            "machine sharing this folder sees it as in use.")


def _take_lease() -> None:
    """Write our record and start the heartbeat. Caller holds _LEASE_LOCK."""
    import atexit
    from . import __version__
    now = _now()
    rec = {"machine": _machine(), "name": _machine_name(), "host": _host(), "pid": os.getpid(),
           "since": now, "heartbeat": now, "version": __version__}
    _write_lease(rec)
    _LEASE.update(mine=rec, beat_at=time.time(), last=None, other=None, lapsed=None, seen=None,
                  fenced=not _is_default(data_root()))
    if _LEASE["stop"] is None:
        stop = _LEASE["stop"] = threading.Event()
        threading.Thread(target=_heartbeat, args=(stop,), name="dandelion-lease",
                         daemon=True).start()
    if not _LEASE["exit_hook"]:
        _LEASE["exit_hook"] = True
        atexit.register(_release_lease)


def acquire_lease() -> bool:
    """At serve start: take the data folder's lease, or, when it is held (another
    machine's live lease, another live process here, or a lease that can't be
    read), open read-only (every check_writable raises StoreInUse). A refused
    root gets no lease. Idempotent. Returns whether this process may save."""
    import sys
    with _LEASE_LOCK:
        if _LEASE["mine"] or _LEASE["other"] or _LEASE["lapsed"] is not None:
            return not _in_use_refusal()
        if _root_refusal():
            return False
        ok, notice = _judge(read_lease(), start=True)
        if not ok:
            print(f"[dandelion] WARNING: {lease_banner()}", file=sys.stderr)
            return False
        try:
            _take_lease()
        except OSError as e:
            print(f"[dandelion] WARNING: could not write {_lease_file()} ({e.strerror or e}); "
                  "another machine sharing this folder will not see it is open here.",
                  file=sys.stderr)
            return True
        if notice:
            _LEASE["notice"] = notice
            print(f"[dandelion] {notice}", file=sys.stderr)
        return True


def retaking_own_lease() -> bool:
    """True while take_lease reloads for a lease still holding our own last record:
    nobody took over, so dialogs in memory may be kept if the store is unchanged."""
    return _LEASE["own_retake"]


def _still_in_use() -> str:
    rec = _LEASE["other"]
    msg = f"Still in use by {_name(rec)}."
    seen = _LEASE["seen"]
    if "machine" not in rec or not seen or rec["machine"] == _machine():
        return msg + " If that is wrong, use Take over."
    left = max(0.0, LEASE_STALE_S - (time.monotonic() - seen[1]))
    at = datetime.fromtimestamp(time.time() + left).strftime("%H:%M")
    return msg + (f" If {_name(rec)} has quit or crashed, Check again works once its lease has "
                  f"stayed unchanged for {int(LEASE_STALE_S // 60)} min (from {at}); "
                  "or use Take over.")


def take_lease(force: bool = False, before=None) -> tuple[bool, str]:
    """Leave read-only. 'Check again' (force False) re-reads the lease and takes it
    only if it was released, is ours, or has not changed for LEASE_STALE_S while
    this Mac watched; after a lapse it first waits LEASE_GRACE_S for the sync.
    'Take over' (force True) takes it anyway. `before` runs just before, still
    read-only: the app reloads its dialogs from disk there, since the ones in
    memory may predate the other machine's saves (retaking_own_lease() says when
    they may be kept). Returns (ok, message)."""
    with _LEASE_LOCK:
        if _root_refusal():
            return False, "The data folder is refused; see the banner."
        if not (_LEASE["other"] or _LEASE["lapsed"] is not None):
            return True, "This machine already has the data folder open."
        if not force and _LEASE["lapsed"] is not None:
            wait = LEASE_GRACE_S - (time.monotonic() - _LEASE["lapsed"])
            if wait > 0:
                return False, (f"Waiting for the other Mac's changes to sync: press Check again "
                               f"in {int(wait) + 1} s.")
        rec = read_lease()
        ok, notice = _judge(rec, start=False)
        if not force and not ok:
            _LEASE["lapsed"] = None            # the holder is known now: show who it is
            return False, _still_in_use()
        _LEASE["own_retake"] = _LEASE["lapsed"] is not None and _is_mine(rec, _LEASE["last"])
        try:
            if before:
                before()
        finally:
            _LEASE["own_retake"] = False
        try:
            _take_lease()
        except OSError as e:
            return False, f"Could not write {_lease_file()}: {e.strerror or e}"
        _LEASE["notice"] = notice if ok else None
        return True, " ".join(filter(None, (
            _LEASE["notice"], "This machine has the data folder now.")))


def _tick(stop: threading.Event | None = None) -> bool:
    """One heartbeat: refresh our lease, unless the hold lapsed or the lease is no
    longer ours. Someone else's record there means they took over: go read-only
    at once and never write ours back over it (only the reload parent, whose
    worker took the lease, steps back quietly). An unreadable lease skips the
    beat. The reads and the write run outside _LEASE_LOCK (a hung mount must not
    block saves or requests); a write that took until past the fence may have
    landed over a take-over, so it counts as a lapse. Returns whether to keep beating."""
    import sys
    with _LEASE_LOCK:
        mine, prev = _LEASE["mine"], _LEASE["beat_at"]
        if not mine or (stop is not None and stop is not _LEASE["stop"]) or _check_lapse():
            return False
    rec = read_lease()
    with _LEASE_LOCK:
        if _LEASE["mine"] is not mine:
            return False                               # changed meanwhile (lapse, reset)
        if rec is _UNREADABLE:
            return True
        if rec is not None and not _is_mine(rec, mine):
            if _LEASE["stop"]:
                _LEASE["stop"].set()
            _LEASE.update(mine=None, stop=None)
            if not (rec["machine"] == _machine() and _reload_parent()):
                _LEASE["other"] = rec
                _observed_stale(rec)
                print(f"[dandelion] WARNING: {_name(rec)} took over the data folder. "
                      f"{lease_banner()}", file=sys.stderr)
            return False
    beat = {**mine, "heartbeat": _now()}
    try:
        _write_lease(beat)
        wrote = True
    except OSError:
        wrote = False                                  # a vanished mount: the lapse catches it
    with _LEASE_LOCK:
        if _LEASE["mine"] is not mine:
            return False
        if _LEASE["fenced"] and time.time() - prev >= _FENCE * LEASE_STALE_S:
            back = _read_lease_once()                  # stalled: was it taken meanwhile?
            if isinstance(back, dict) and not _is_mine(back, beat) and not _is_mine(back, mine):
                if _LEASE["stop"]:
                    _LEASE["stop"].set()
                _LEASE.update(mine=None, stop=None, other=back)
                print(f"[dandelion] WARNING: {lease_banner()}", file=sys.stderr)
            else:
                _lapse()
            return False
        if wrote:
            _LEASE.update(mine=beat, beat_at=time.time())
        return True


def _heartbeat(stop: threading.Event) -> None:
    import sys
    warned = False
    while not stop.wait(LEASE_HEARTBEAT_S):
        try:
            if not _tick(stop):
                return
        except Exception as e:  # noqa: BLE001 — never let the beat die (the lapse is the backstop)
            if not warned:
                warned = True
                print(f"[dandelion] WARNING: lease heartbeat failed: {e!r}", file=sys.stderr)


def _release_lease() -> None:
    """At exit: stop beating and remove the lease, only if it is still ours."""
    with _LEASE_LOCK:
        if _LEASE["stop"]:
            _LEASE["stop"].set()
        if _is_mine(_read_lease_once(), _LEASE["mine"]):
            with contextlib.suppress(OSError):
                _lease_file().unlink()
        _LEASE.update(mine=None, stop=None)


def _reset_lease() -> None:
    """Forget any lease state without touching the file (test fixture)."""
    with _LEASE_LOCK:
        if _LEASE["stop"]:
            _LEASE["stop"].set()
        _LEASE.update(mine=None, last=None, other=None, lapsed=None, seen=None, notice=None,
                      stop=None, beat_at=0.0, own_retake=False, fenced=True)


def _maybe_held(rec) -> bool:
    """For one-shot commands, which can't watch the lease: another machine's lease
    stamped within LEASE_UNSEEN_S, or one that can't be read."""
    return rec is _UNREADABLE or (isinstance(rec, dict) and rec["machine"] != _machine()
                                  and _stamp_age(rec) < LEASE_UNSEEN_S)


# ---- backup / restore --------------------------------------------------------
_INDEX = "meta/paper-keys.json"   # upload name -> markdown cache key at backup time


def _walk(base: Path, prefix: str, special: list[Path],
          with_secrets: bool = False) -> list[tuple[Path, str]]:
    out = []
    if not base.is_dir():
        return out
    for p in sorted(base.rglob("*")):
        if not p.is_file() or p.is_symlink():
            continue
        rel = p.relative_to(base)
        if _SKIP_DIRS.intersection(rel.parts[:-1]):
            continue
        if prefix == "data/" and rel.parts[0] == "backups":
            continue                             # the store's own snapshots
        if ".corrupt-" in p.name:
            continue                             # damaged stores set aside
        if _skipped(p.name):
            continue
        if p.name == "secrets.json" and not with_secrets:
            continue                             # stray key files (e.g. old data/) too
        if any(_same(p, s) for s in special):
            continue                             # config is archived under config/
        out.append((p, prefix + rel.as_posix()))
    return out


def _members(with_secrets: bool) -> list[tuple[Path, str]]:
    from .paper import _cache_dir
    root, papers = data_root(), _cache_dir()
    special = list(_config_files().values())
    try:
        papers_inside = papers.resolve().is_relative_to(root.resolve())
    except OSError:
        papers_inside = True
    out = [(p, a) for p, a in _walk(root, "data/", special, with_secrets)
           if not (papers_inside is False and a.startswith("data/papers/"))]
    if not papers_inside:                        # DANDELION_PAPERS elsewhere
        out += _walk(papers, "data/papers/", special, with_secrets)
    cfg = _config_files()
    if not with_secrets:
        cfg.pop("config/secrets.json")
    out += [(p, arc) for arc, p in cfg.items() if p.is_file()]
    return out


def _paper_keys() -> dict[str, str]:
    """Markdown cache keys embed each upload's absolute path + mtime, which a new
    install won't share. Record them so restore can re-key (no re-conversion)."""
    from .paper import _cache_dir, _md_key
    up = _cache_dir() / "uploads"
    keys = {}
    for pdf in sorted(up.glob("*.pdf")) if up.is_dir() else []:
        k = _md_key(pdf)
        if (_cache_dir() / f"{k}.md").is_file():
            keys[pdf.name] = k
    return keys


def backup(dest: str | Path | None = None, with_secrets: bool = False) -> tuple[Path, int]:
    """Write every local data file to one .tar.gz. Returns (path, file count).
    The archive is created 0600, since notebooks can hold private content."""
    import io
    import json
    stamp = time.strftime("%Y%m%d-%H%M%S")
    dest = Path(dest).expanduser() if dest else Path.cwd() / f"dandelion-backup-{stamp}.tar.gz"
    if dest.is_dir():
        dest = dest / f"dandelion-backup-{stamp}.tar.gz"
    members = _members(with_secrets)
    index = json.dumps(_paper_keys(), indent=1).encode()
    fd = os.open(dest, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "wb") as fh, tarfile.open(fileobj=fh, mode="w:gz") as tar:
        for p, arc in members:
            tar.add(p, arcname=arc, recursive=False)
        info = tarfile.TarInfo(_INDEX)
        info.size, info.mtime = len(index), int(time.time())
        tar.addfile(info, io.BytesIO(index))
    return dest, len(members)


def _target_for(name: str) -> Path | None:
    """Map an archive member name to where it restores, or None if unsafe."""
    from .paper import _cache_dir
    pp = PurePosixPath(name)
    if pp.is_absolute() or ".." in pp.parts or len(pp.parts) < 2:
        return None
    cfg = _config_files(write=True)
    if name in cfg:
        return cfg[name]
    if pp.parts[0] != "data":
        return None
    if pp.parts[1] == "papers":
        if len(pp.parts) < 3:
            return None                          # would replace the papers dir
        return _cache_dir().joinpath(*pp.parts[2:])
    return data_root().joinpath(*pp.parts[1:])


def ensure_stopped() -> None:
    """Raise AppRunning while the app (here or on another machine) has this folder open."""
    pid = running_server_pid()
    if pid:
        # the app holds notebooks in memory and rewrites the store on every
        # edit, so restoring under it would be silently undone
        raise AppRunning(f"the app is running (pid {pid}); stop it first (just stop). "
                         f"If it isn't, delete {_pidfile()}")
    rec = read_lease()
    if _maybe_held(rec):
        who = (f"{_name(rec)} ({_seen_ago(rec)})" if isinstance(rec, dict)
               else "a machine whose lease file can't be read")
        raise AppRunning(f"Dandelion is open on {who} with this data folder; stop it there "
                         f"first. If it isn't running, delete {_lease_file()}")


def restore(archive: str | Path, force: bool = False) -> tuple[list[str], list[str]]:
    """Unpack a backup into this install. Existing files are kept unless
    `force`. Returns (restored, skipped). Run it with the app stopped: the app
    loads notebooks at startup and would overwrite them on its next save."""
    import json
    from .paper import _cache_dir, _md_key
    from .secrets_store import secrets_path
    ensure_stopped()
    check_writable()
    secret = secrets_path(write=True)
    restored, skipped, index = [], [], {}
    with tarfile.open(Path(archive).expanduser(), "r:gz") as tar:
        for m in tar.getmembers():
            if m.isdir():
                continue
            if not m.isfile():
                skipped.append(f"{m.name} (not a regular file)")
                continue                         # no links or devices
            if m.name == _INDEX:
                try:
                    index = json.loads(tar.extractfile(m).read())
                except ValueError:
                    index = {}
                continue
            dst = _target_for(m.name)
            if _skipped(PurePosixPath(m.name).name):
                skipped.append(f"{m.name} (belongs to the machine that made the backup)")
                continue
            if dst is None:
                skipped.append(f"{m.name} (unsafe path)")
                continue
            if dst.is_dir() or (dst.exists() and not force):
                skipped.append(f"{m.name} ({'is a directory' if dst.is_dir() else 'exists'})")
                continue
            make_dirs(dst.parent)
            tmp = dst.with_name(dst.name + ".restore-tmp")
            try:
                fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
                with os.fdopen(fd, "wb") as out:
                    shutil.copyfileobj(tar.extractfile(m), out)
                if not _same(dst, secret) and m.name != "config/secrets.json":
                    os.chmod(tmp, 0o644)         # keys stay 0600 whatever they're called
                os.utime(tmp, (m.mtime, m.mtime))
                tmp.replace(dst)
            except OSError as e:
                tmp.unlink(missing_ok=True)
                skipped.append(f"{m.name} ({e.strerror or e})")
                continue
            restored.append(m.name)
    for name in restored:
        pp = PurePosixPath(name)
        if len(pp.parts) == 2 and re.fullmatch(r"dialogs-.+(?<!\.local)\.json", pp.name) \
                and data_root().joinpath(pp.name[:-5] + ".d").is_dir():
            import sys
            print(f"[dandelion] WARNING: restored {pp.name} next to its dialog folder; the "
                  "folder wins, and the dialogs only the file has are added at the next start. "
                  "Nothing is rolled back.", file=sys.stderr)
    # re-key converted markdown to this install's upload paths
    cache = _cache_dir()
    for name, old_key in (index.items() if isinstance(index, dict) else []):
        pdf, md = cache / "uploads" / str(name), cache / f"{old_key}.md"
        if not (isinstance(old_key, str) and old_key.isalnum() and "/" not in str(name)):
            continue                             # a crafted index can't point outside
        if not (pdf.is_file() and md.is_file()):
            continue
        new_md = cache / f"{_md_key(pdf)}.md"
        if not new_md.exists():
            shutil.copy2(md, new_md)
    # re-point "reopen paper" links that name an upload under the archive's
    # original root (another machine, a pre-rename ~/.config/pappus) to this one
    up = cache / "uploads"

    def repoint(path: str) -> str | None:
        p = Path(path)
        if p.is_absolute() and p.parent.name == "uploads" and not p.exists() \
                and (up / p.name).is_file():
            return str(up / p.name)
        return None
    try:
        _rewrite_sources(cache / "sources.json", repoint)
    except (OSError, ValueError):
        pass
    return restored, skipped
