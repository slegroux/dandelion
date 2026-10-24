"""Where your notebooks and everything else you create live — and how to move them.

All user data is local to the machine and never part of the git repo:

    ~/.config/dandelion/          (override with DANDELION_DATA)
        dialogs-<target>.json            notebooks: cells, outputs, sidebar list
        dialogs-<target>.local.json      dialogs marked "keep local only"
        dialogs-<target>.private         names of those dialogs
        recall.json                      spaced-repetition schedule
        libraries.json                   registered nbdev libraries
        blog/                            the Quarto blog project
        papers/                          reading library (DANDELION_PAPERS)
        tools.json, secrets.json         Ask-AI tool policy, API keys

`backup()` packs that into one .tar.gz and `restore()` unpacks it on a new
install (`dandelion backup` / `dandelion restore`). API keys are left out of a
backup unless asked for, so an archive can be copied around safely.

`migrate_repo_data()` moves an old in-repo store (`<repo>/data`, which the
justfile used to point PAPPUS_DATA at and `just sync` pushed to GitHub) into
the local home once, never overwriting anything already there.
"""
from __future__ import annotations

import contextlib
import os
import re
import shutil
import tarfile
import time
from pathlib import Path, PurePosixPath

REPO_DATA = Path(__file__).resolve().parent.parent / "data"

# rolling save-backups and half-written temp files are not worth carrying over
_SKIP_SUFFIXES = (".bak", ".tmp", ".restore-tmp", ".tar.gz")   # .tar.gz: older backups
_SKIP_NAMES = {".DS_Store", ".serve.pid"}
_SKIP_DIRS = {".git", "__pycache__", ".ipynb_checkpoints", ".quarto"}


def data_root() -> Path:
    base = os.environ.get("DANDELION_DATA")
    return Path(base).expanduser() if base else Path.home() / ".config" / "dandelion"


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
    if not old.is_dir() or _same(old, new):
        return []
    new.mkdir(parents=True, exist_ok=True)
    moved = [f"papers/{m}" for m in paper.migrate_legacy_cache(old / "papers")]
    for src in sorted(old.iterdir()):
        if src.name == "papers" or src.name in _SKIP_NAMES:
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
        if p.is_file() and p.name not in _SKIP_NAMES and not p.name.endswith(_SKIP_SUFFIXES) \
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
            "DANDELION_DATA=/tmp/dandelion-test DANDELION_PORT=8002 "
            "uv run python -m dandelion.cli serve. "
            f"If no server is running, delete {_pidfile()}")


def claim_serving() -> None:
    """Check that no other live server holds this data root and record this
    `dandelion serve` process in .serve.pid (restore refuses to run under it).
    Raises AppRunning when another server is live, or when the record can't be
    written: an unrecorded server would leave the folder open to a second one."""
    import atexit
    f, me = _pidfile(), os.getpid()
    other = running_server_pid()
    if other:
        raise AppRunning(serve_refusal(other))
    tmp = f.with_name(f"{f.name}.{me}.tmp")          # unique, and skipped by backups
    try:
        f.parent.mkdir(parents=True, exist_ok=True)
        tmp.write_text(str(me))
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
                    fh.write(str(me))
        finally:
            tmp.unlink(missing_ok=True)
    except OSError as e:
        raise AppRunning(f"not starting: could not record this server in {f} ({e.strerror or e}). "
                         "Without that record a second server could start on this folder. "
                         "Fix the folder's permissions, or use another DANDELION_DATA") from e

    def _clear():
        try:
            if f.read_text().strip() == str(me):
                f.unlink()
        except OSError:
            pass
    atexit.register(_clear)


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
    """PID of a live `dandelion serve` using this data root (or `root`), else None."""
    try:
        pid = int(((root / ".serve.pid") if root else _pidfile()).read_text().strip())
    except (OSError, ValueError):
        return None
    return pid if pid != os.getpid() and _pid_alive(pid) and _looks_like_dandelion(pid) else None


class AppRunning(RuntimeError):
    pass


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
        if p.name in _SKIP_NAMES or p.name.endswith(_SKIP_SUFFIXES):
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


def restore(archive: str | Path, force: bool = False) -> tuple[list[str], list[str]]:
    """Unpack a backup into this install. Existing files are kept unless
    `force`. Returns (restored, skipped). Run it with the app stopped: the app
    loads notebooks at startup and would overwrite them on its next save."""
    import json
    from .paper import _cache_dir, _md_key
    from .secrets_store import secrets_path
    pid = running_server_pid()
    if pid:
        # the app holds notebooks in memory and rewrites the store on every
        # edit, so restoring under it would be silently undone
        raise AppRunning(f"the app is running (pid {pid}); stop it first (just stop). "
                         f"If it isn't, delete {_pidfile()}")
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
            if dst is None:
                skipped.append(f"{m.name} (unsafe path)")
                continue
            if dst.is_dir() or (dst.exists() and not force):
                skipped.append(f"{m.name} ({'is a directory' if dst.is_dir() else 'exists'})")
                continue
            dst.parent.mkdir(parents=True, exist_ok=True)
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
