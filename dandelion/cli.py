"""dandelion CLI — manage tunnels, diagnose connections, launch the UI.

Usage:
    dandelion targets                 list configured targets
    dandelion doctor [name]           diagnose a target's connection
    dandelion up <name>               open the SSH tunnel for a remote target (blocks)
    dandelion serve                   launch the web UI (http://localhost:8000)
    dandelion library build <lib> <dest> [--pkg <name>]
                                     project cells tagged `#| export <lib>:<mod>`
                                     across all dialogs into an nbdev project at
                                     <dest>, then build it with nbdev if installed
    dandelion blog build <dialog> <dest> [--title <t>] [--author <a>]
    dandelion blog build --all <dest>   [--title <t>] [--author <a>]
                                     project a dialog (or every dialog with
                                     --all) into a Quarto blog at <dest> — one
                                     post each, outputs kept — and render it with
                                     quarto if installed. --author defaults to the
                                     git user.name
    dandelion backup [path] [--with-secrets]
                                     pack notebooks, papers, recall, libraries,
                                     blog and tool config into one .tar.gz (API
                                     keys only with --with-secrets)
    dandelion restore <archive> [--force]
                                     unpack a backup into this install (stop the
                                     app first); existing files kept unless --force

All of that lives locally in ~/.config/dandelion (DANDELION_DATA), never
in the git repo.
"""
from __future__ import annotations

import os
import sys

from .targets import get_target, list_targets, load_config


def cmd_targets(_):
    cfg = load_config()
    default = cfg.get("default")
    for name in list_targets():
        t = get_target(name)
        kind = f"remote via {t.ssh.host}" if t.is_remote else "local"
        star = " (default)" if name == default else ""
        print(f"  {name:8} {t.url:28} [{kind}]{star}")
    return 0


def cmd_doctor(args):
    from .doctor import run_checks
    name = args[0] if args else None
    t = get_target(name)
    print(f"Diagnosing target '{t.name}' -> {t.url}\n")
    ok_all = True
    for ok, label, detail in run_checks(t):
        print(f"  [{'OK ' if ok else 'XX '}] {label:7} {detail}")
        ok_all = ok_all and ok
    print("\n" + ("All checks passed." if ok_all else "Some checks failed — see above."))
    return 0 if ok_all else 1


def cmd_up(args):
    from .tunnel import open_tunnel, close_tunnel
    if not args:
        print("usage: dandelion up <target>", file=sys.stderr)
        return 2
    t = get_target(args[0])
    if not t.is_remote:
        print(f"Target '{t.name}' is local — no tunnel needed.")
        return 0
    print(f"Opening tunnel {t.ssh.host}:{t.ssh.remote_port} -> localhost:{t.ssh.local_port} …")
    proc = open_tunnel(t)
    print(f"Tunnel up. Point the UI at target '{t.name}'. Ctrl-C to close.")
    try:
        proc.wait()
    except KeyboardInterrupt:
        print("\nClosing tunnel…")
        close_tunnel(proc)
    return 0


def _ignore_repo_data_env():
    """Old launchers set PAPPUS_DATA=<repo>/data (SIDEKICK_DATA before that);
    user data no longer lives there. All three names go: the package aliases the
    old ones to DANDELION_DATA on import, so a leftover would come back in any
    child that imports it (uvicorn's reload worker)."""
    from . import datadir
    names = ("DANDELION_DATA", "PAPPUS_DATA", "SIDEKICK_DATA")
    stale = {k: v for k in names if (v := os.environ.get(k))
             and datadir._same(datadir.Path(v).expanduser(), datadir.REPO_DATA)}
    if not stale:
        return
    for k in stale:
        del os.environ[k]
    for k in ("PAPPUS_DATA", "SIDEKICK_DATA"):   # what a child's alias would pick
        if k in os.environ:
            os.environ.setdefault("DANDELION_DATA", os.environ[k])
    # name what the user set: DANDELION_DATA only if it isn't just the alias
    old = [k for k in stale if k != "DANDELION_DATA"]
    if "DANDELION_DATA" in stale and stale["DANDELION_DATA"] not in (stale[k] for k in old):
        old.insert(0, "DANDELION_DATA")
    print(f"note: ignoring {', '.join(f'{k}={stale[k]}' for k in old)} (inside the repo); "
          f"using {datadir.data_root()}", file=sys.stderr)


def _adopt_legacy_root():
    """Move ~/.config/pappus (or solveit-sidekick) to ~/.config/dandelion once,
    and say what happened. Never raises; returns False only when an older
    server still runs on the old folder, so backup/restore would miss the data."""
    from . import datadir
    try:
        adopted = datadir.adopt_legacy_root()
    except datadir.MigrationNotice as e:
        print(f"warning: {e}", file=sys.stderr)
        return not isinstance(e, datadir.LegacyAppRunning)
    except Exception as e:  # noqa: BLE001 — a failed move must never block serve
        print(f"warning: could not move the old data folder to ~/.config/dandelion: {e}",
              file=sys.stderr)
        return True
    if adopted and adopted.is_symlink():
        print(f"Linked {adopted} to {os.readlink(adopted)}, where your old data folder points")
    elif adopted:
        print(f"Moved your data to {adopted} (the old folder now links there)")
    return True


def _move_out_of_repo() -> bool:
    """User data used to live in <repo>/data (pushed to GitHub by `just sync`).
    Ignore a stale data env var still pointing there, adopt an old ~/.config
    root, and move what's in <repo>/data into the local data root once.
    Returns False, having changed nothing, while an older server still runs on
    the old folder: starting now would create an empty ~/.config/dandelion and
    strand the notebooks in the old one."""
    from . import datadir
    _ignore_repo_data_env()
    if not _adopt_legacy_root():
        return False
    try:
        datadir.rekey_papers()     # idempotent: finishes a re-key a crash cut short
    except Exception as e:  # noqa: BLE001 — a missed re-key only costs a re-conversion
        print(f"warning: could not re-key converted papers: {e}", file=sys.stderr)
    try:
        moved = datadir.migrate_repo_data()
    except Exception as e:  # noqa: BLE001 — a failed migration must never block serve
        print(f"warning: could not move {datadir.REPO_DATA} to {datadir.data_root()}: {e}",
              file=sys.stderr)
        return True
    if moved:
        print(f"Moved {len(moved)} entries from {datadir.REPO_DATA} to {datadir.data_root()}")
    left = datadir.leftovers()
    if left:
        print(f"warning: {len(left)} files in {datadir.REPO_DATA} were not moved because "
              f"{datadir.data_root()} already has them: {', '.join(left[:5])}"
              f"{' …' if len(left) > 5 else ''}", file=sys.stderr)
    return True


def cmd_backup(args):
    """`dandelion backup [path] [--with-secrets]` — one portable .tar.gz."""
    from . import datadir
    _ignore_repo_data_env()
    if not _adopt_legacy_root():
        print("backup refused: stop the older server first (see above)", file=sys.stderr)
        return 1
    refused = datadir.store_refusal()
    if refused:
        print(f"backup refused: {refused}", file=sys.stderr)
        return 1
    with_secrets = "--with-secrets" in args
    rest = [a for a in args if a != "--with-secrets"]
    dest, n = datadir.backup(rest[0] if rest else None, with_secrets=with_secrets)
    print(f"Backed up {n} files from {datadir.data_root()} -> {dest}")
    if with_secrets:
        print("  includes API keys (secrets.json): keep this archive private")
    return 0


def cmd_restore(args):
    """`dandelion restore <archive> [--force]` — unpack a backup here."""
    from . import datadir
    _ignore_repo_data_env()
    if not _adopt_legacy_root():
        print("restore refused: stop the older server first (see above)", file=sys.stderr)
        return 1
    force = "--force" in args
    rest = [a for a in args if a != "--force"]
    if not rest:
        print("usage: dandelion restore <archive> [--force]", file=sys.stderr)
        return 2
    try:
        restored, skipped = datadir.restore(rest[0], force=force)
    except (datadir.AppRunning, datadir.StoreRefused) as e:
        print(f"restore refused: {e}", file=sys.stderr)
        return 1
    print(f"Restored {len(restored)} files into {datadir.data_root()}")
    if skipped:
        print(f"Skipped {len(skipped)}: {', '.join(skipped[:8])}{' …' if len(skipped) > 8 else ''}")
        if any(s.endswith("(exists)") for s in skipped):
            print("  (re-run with --force to overwrite existing files)")
    return 0


def cmd_serve(_):
    import uvicorn

    from . import datadir
    # Default to loopback: the UI is single-user with global state and holds your
    # API keys. Opt into network exposure explicitly via DANDELION_HOST=0.0.0.0.
    host = os.environ.get("DANDELION_HOST", "127.0.0.1")
    port = int(os.environ.get("DANDELION_PORT", "8000"))
    # DANDELION_RELOAD=1 → auto-restart on source edits (dev convenience). Reload
    # needs an import string rather than the app object so uvicorn can re-import
    # the module in the worker; without it we pass the object directly.
    reload = os.environ.get("DANDELION_RELOAD", "").lower() in ("1", "true", "yes")
    _ignore_repo_data_env()
    if host not in ("127.0.0.1", "localhost"):
        # The app has no auth layer of its own, so a non-loopback bind exposes
        # unauthenticated code execution and your stored API keys to the network.
        # There is deliberately no env-var escape hatch (a token that gated the
        # bind but enforced no auth would be false security). For remote access,
        # forward the loopback port over SSH instead:
        #   ssh -L 8000:127.0.0.1:8000 <host>   # then open http://127.0.0.1:8000
        raise SystemExit(
            f"refusing to bind {host}: the UI has no authentication, so this would "
            "expose unauthenticated code execution and your stored API keys. "
            "Use an SSH tunnel for remote access (see the comment in cli.py).")
    # before anything writes to the folder (migrations, the "Dandelion UI" line)
    other = datadir.running_server_pid()
    if other:
        raise SystemExit(datadir.serve_refusal(other))
    print(f"Dandelion UI -> http://{host}:{port}" + ("  (auto-reload)" if reload else ""))
    if not _move_out_of_repo():
        raise SystemExit("not starting: stop the older server first (see above)")
    datadir.freeze_root()          # the root and any refusal hold until restart
    try:
        datadir.claim_serving()
    except datadir.AppRunning as e:
        raise SystemExit(str(e)) from None
    if reload:
        # the worker holds the dialogs, so it takes the lease over from this process
        os.environ["_DANDELION_RELOAD_WORKER"] = str(os.getpid())
        uvicorn.run("dandelion.app:app", host=host, port=port,
                    reload=True, reload_dirs=[os.path.dirname(__file__)])
    else:
        from .app import app
        uvicorn.run(app, host=host, port=port)
    return 0


def cmd_library(args):
    """`dandelion library build <lib> <dest> [--pkg <name>]` — emit + build a library."""
    from .client import connect
    from . import nbdev_export
    if len(args) < 3 or args[0] != "build":
        print("usage: dandelion library build <lib> <dest> [--pkg <name>]", file=sys.stderr)
        return 2
    lib, dest = args[1], args[2]
    pkg = None
    if "--pkg" in args:
        i = args.index("--pkg")
        pkg = args[i + 1] if i + 1 < len(args) else None
    backend, warning = connect(get_target())
    if warning:
        print(f"  ⚠ {warning}")
    result = nbdev_export.build_library(backend, lib, dest, pkg)
    if not result["modules"]:
        print(f"No cells tagged `#| export {lib}:<module>` found across dialogs — "
              f"emitted an empty scaffold at {dest}.")
    else:
        print(f"Built library '{result['pkg']}' at {dest}")
        print(f"  modules: {', '.join(result['modules'])}")
        print(f"  files:   {len(result['files'])}")
    print(f"  nbdev:   {'✓ ' if result['nbdev_ok'] else '· '}{result['nbdev_detail']}")
    return 0


def cmd_blog(args):
    """`dandelion blog build <dialog> <dest> [--title T]` (or `--all <dest>`)."""
    from datetime import date as _date
    from . import blog
    from .client import connect
    if not args or args[0] != "build":
        print("usage: dandelion blog build <dialog> <dest> [--title <t>] [--author <a>]\n"
              "       dandelion blog build --all <dest> [--title <t>] [--author <a>]", file=sys.stderr)
        return 2
    rest = args[1:]
    title = "Dandelion"
    if "--title" in rest:
        i = rest.index("--title")
        title = rest[i + 1] if i + 1 < len(rest) else title
        del rest[i:i + 2]
    author = None            # None → build_blog defaults it from git user.name
    if "--author" in rest:
        i = rest.index("--author")
        author = rest[i + 1] if i + 1 < len(rest) else author
        del rest[i:i + 2]
    all_dialogs = "--all" in rest
    rest = [a for a in rest if a != "--all"]

    backend, warning = connect(get_target())
    if warning:
        print(f"  ⚠ {warning}")

    if all_dialogs:
        if len(rest) < 1:
            print("usage: dandelion blog build --all <dest> [--title <t>]", file=sys.stderr)
            return 2
        dest = rest[0]
        dialogs = backend.list_dialogs() or []
        if not dialogs:
            print("No dialogs found — nothing to publish.")
            return 0
    else:
        if len(rest) < 2:
            print("usage: dandelion blog build <dialog> <dest> [--title <t>]", file=sys.stderr)
            return 2
        dialogs, dest = [rest[0]], rest[1]

    result = blog.build_blog(backend, dialogs, dest, title=title,
                             date=_date.today().isoformat(), author=author)
    print(f"Built blog '{title}' at {dest}")
    print(f"  posts: {', '.join(result['posts'])}")
    print(f"  files: {len(result['files'])}")
    print(f"  quarto: {'✓ ' if result['render_ok'] else '· '}{result['render_detail']}")
    return 0


COMMANDS = {"targets": cmd_targets, "doctor": cmd_doctor, "up": cmd_up,
            "serve": cmd_serve, "library": cmd_library, "blog": cmd_blog,
            "backup": cmd_backup, "restore": cmd_restore}


def main(argv=None):
    argv = argv if argv is not None else sys.argv[1:]
    if not argv or argv[0] not in COMMANDS:
        print(__doc__)
        return 0 if not argv else 2
    return COMMANDS[argv[0]](argv[1:])


if __name__ == "__main__":
    raise SystemExit(main())
