"""Dashboard build freshness/serialization and checkout bytecode sweep.

Split out of ``hermes_cli/main.py``. Names that still live in main (``PROJECT_ROOT``, ...)
are imported lazily inside the functions that use them (avoids an import cycle).
"""

import logging
import os
import subprocess
import sys

from pathlib import Path

# Log-record parity with the origin module.
logger = logging.getLogger("hermes_cli.main")

# Checkout fingerprint the bytecode cache was last validated against. Lives next
# to the checkout (NOT in HERMES_HOME): __pycache__ is per-checkout state shared
# by every profile.
_BYTECODE_FINGERPRINT_FILE = ".bytecode-fingerprint"


def _record_bytecode_fingerprint() -> None:
    """Persist the current checkout fingerprint after a bytecode sweep. Never raises."""
    from hermes_cli.main import PROJECT_ROOT, _read_git_revision_fingerprint
    try:
        fingerprint = _read_git_revision_fingerprint(PROJECT_ROOT)
        if not fingerprint:
            return
        stamp_path = PROJECT_ROOT / _BYTECODE_FINGERPRINT_FILE
        tmp_path = stamp_path.with_name(stamp_path.name + ".tmp")
        tmp_path.write_text(fingerprint, encoding="utf-8")
        tmp_path.replace(stamp_path)
    except OSError as exc:
        logger.debug("Could not record bytecode fingerprint: %s", exc)


def _sweep_stale_bytecode_if_checkout_changed() -> None:
    """Clear ``__pycache__`` at launch when the checkout fingerprint changed since the last sweep.

    Update-time clears can't close the stale-bytecode class: ``hermes update`` runs
    the PRE-pull updater code and manual pulls never run it. Cheap file reads, no
    git subprocess. Never raises.

    The stale-bytecode bug class (issues #6207, #60242; Dhruv's WhatsApp ``cannot import name
    'parse_model_flags_detailed'`` report) has one shared shape: the checkout's ``.py`` files change (git
    pull inside ``hermes update``, a manual ``git pull``, a ZIP update, a file-sync restore) while
    ``__pycache__`` retains bytecode from the previous revision, and a later process trusts the stale
    ``.pyc`` instead of the fresh source.
    """
    from hermes_cli.main import PROJECT_ROOT, _clear_bytecode_cache, _read_git_revision_fingerprint
    try:
        fingerprint = _read_git_revision_fingerprint(PROJECT_ROOT)
        if not fingerprint:
            return  # non-git install — the ZIP update path clears explicitly
        stamp_path = PROJECT_ROOT / _BYTECODE_FINGERPRINT_FILE
        try:
            recorded = stamp_path.read_text(encoding="utf-8-sig").strip()
        except OSError:
            recorded = ""
        if recorded == fingerprint:
            return
        removed = _clear_bytecode_cache(PROJECT_ROOT)
        if removed:
            logger.info(
                "Checkout changed since last launch (%s -> %s): cleared %d stale __pycache__ director%s",
                recorded or "unknown", fingerprint, removed, "y" if removed == 1 else "ies",
            )
        _record_bytecode_fingerprint()
    except Exception as exc:
        logger.debug("Stale-bytecode launch sweep failed: %s", exc)


def _web_project_root(web_dir: Path) -> Path:
    """Repo root for a frontend dir (``web/`` or ``apps/<name>/``)."""
    return web_dir.parent.parent if web_dir.parent.name == "apps" else web_dir.parent


def _web_dist_dir(web_dir: Path) -> Path:
    """Vite outputs to ``hermes_cli/web_dist/`` (vite.config.ts outDir), NOT ``web/dist/``."""
    return _web_project_root(web_dir) / "hermes_cli" / "web_dist"


def _web_ui_build_needed(web_dir: Path) -> bool:
    from hermes_cli.source_build import source_product_current

    return not source_product_current(_web_project_root(web_dir), "web", _web_dist_dir(web_dir))


def _write_web_ui_build_stamp(project_root: Path, web_dir: Path) -> None:
    """Historical updater entrypoint; current builders publish their own receipts."""
    from hermes_cli._old_updater import stop_for_relaunch
    stop_for_relaunch()


def _console_print(text: str) -> None:
    """print() that survives cp1252-style consoles (arrow/check glyphs) via errors="replace"."""
    try:
        print(text)
    except UnicodeEncodeError:
        encoding = getattr(sys.stdout, "encoding", None) or "ascii"
        print(text.encode(encoding, errors="replace").decode(encoding, errors="replace"))


def _run_with_idle_timeout(
    cmd: list[str], cwd: Path, *, idle_timeout_seconds: int = 180, indent: str = "    ",
    env: dict[str, str] | None = None) -> subprocess.CompletedProcess:
    """Stop an old updater instead of running the retired build path."""
    from hermes_cli._old_updater import stop_for_relaunch
    stop_for_relaunch()


def _nixos_build_env() -> dict[str, str] | None:
    """Stop an old updater instead of running the retired build path."""
    from hermes_cli._old_updater import stop_for_relaunch
    stop_for_relaunch()


def _fetch_steamos_glibc_headers(cache_dir: Path, glibc_version: str | None) -> bool:
    """Download and unpack glibc + linux-api-headers into *cache_dir*.

    Fetches the exact-matching packages from the distro's own package
    mirror (the URL ``pacman -Sp`` prints) via curl, then unpacks with the
    system ``tar --zstd`` (Python's stdlib ``tarfile`` has no zstd
    support). Returns whether the cache is now populated and usable.
    """
    from hermes_platform.resolver.core import locate_command
    if locate_command("tar").kind == "missing":
        return False
    try:
        cache_dir.mkdir(parents=True, exist_ok=True)
        for pkg in ("glibc", "linux-api-headers"):
            url_result = subprocess.run(
                ["pacman", "-Sp", pkg],
                capture_output=True, text=True, encoding="utf-8", errors="replace",
                check=False, timeout=15,
            )
            url = url_result.stdout.strip().splitlines()[-1] if url_result.returncode == 0 else None
            if not url:
                return False
            archive = cache_dir / f"{pkg}.pkg.tar.zst"
            dl = subprocess.run(
                ["curl", "-fsSL", "-o", str(archive), url],
                check=False, timeout=120,
            )
            if dl.returncode != 0 or not archive.exists():
                return False
            extract = subprocess.run(
                ["tar", "--zstd", "-xf", str(archive), "-C", str(cache_dir)],
                check=False, timeout=60,
            )
            archive.unlink(missing_ok=True)
            if extract.returncode != 0:
                return False
        if glibc_version:
            (cache_dir / ".glibc-version").write_text(glibc_version, encoding="utf-8")
        return True
    except Exception:
        return False


def _steamos_glibc_header_env() -> dict[str, str] | None:
    """Return extra CFLAGS/CXXFLAGS for native module builds on SteamOS.

    SteamOS's immutable root ships GCC's own header shims in
    ``/usr/include`` but not the full glibc/kernel dev headers
    (``stdint.h``, ``linux/types.h``, etc.), and ``/usr/include`` is
    read-only so they can't be installed in place. node-gyp's C++ compile
    for native addons (node-pty's ``pty.cc``) then fails with ``fatal
    error: stdint.h: No such file or directory`` even though gcc/g++ are
    present, because the ``#include_next`` header chain has a gap.

    Fetches the exact-matching ``glibc``/``linux-api-headers`` packages
    from the distro's own mirror into a machine-local cache under
    ``~/.hermes/cache/steamos-glibc-headers`` (re-fetched automatically
    when the installed ``glibc`` package version drifts from the cached
    one — a stale cache would carry version-mismatched prototypes), and
    points the compiler at that tree with ``-idirafter`` — appended to the
    END of the search path, so it never shadows a header that already
    exists in ``/usr/include``. ``CPATH`` would instead PREPEND and break
    GCC's own header shim chaining.

    Returns an env dict suitable for ``subprocess.run(env=...)``, or
    ``None`` when we are not on SteamOS, ``pacman``/``curl``/``tar`` are
    unavailable, or the fetch fails — same fail-open contract as
    :func:`_nixos_build_env`, falling through to node-gyp's normal
    (likely-failing) behavior rather than raising.
    """
    import re

    try:
        os_release = Path("/etc/os-release").read_text(encoding="utf-8")
    except OSError:
        return None
    if not re.search(r"^ID=steamos$", os_release, re.M):
        return None

    from hermes_platform.resolver.core import locate_command
    if any(locate_command(_t).kind == "missing" for _t in ("pacman", "curl")):
        return None

    from hermes_constants import get_hermes_home
    cache_dir = get_hermes_home() / "cache" / "steamos-glibc-headers"
    version_marker = cache_dir / ".glibc-version"
    include_dir = cache_dir / "usr" / "include"

    try:
        glibc_q = subprocess.run(
            ["pacman", "-Q", "glibc"],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            check=False, timeout=10,
        )
        current_version = glibc_q.stdout.strip().split()[-1] if glibc_q.returncode == 0 else None
    except Exception:
        current_version = None

    cached_version = None
    if version_marker.exists():
        try:
            cached_version = version_marker.read_text(encoding="utf-8").strip()
        except OSError:
            cached_version = None

    stale = bool(current_version) and cached_version != current_version
    if not (include_dir / "stdint.h").exists() or stale:
        if not _fetch_steamos_glibc_headers(cache_dir, current_version):
            return None

    if not (include_dir / "stdint.h").exists():
        return None

    env = dict(os.environ)
    extra = f"-idirafter {include_dir}"
    for var in ("CFLAGS", "CXXFLAGS"):
        env[var] = f"{env[var]} {extra}" if env.get(var) else extra
    return env


def _run_npm_install_deterministic(
    npm: str, cwd: Path, *, extra_args: tuple[str, ...] = (), capture_output: bool = True,
    env: dict[str, str] | None = None) -> subprocess.CompletedProcess:
    """Stop an old updater instead of running the retired build path."""
    from hermes_cli._old_updater import stop_for_relaunch
    stop_for_relaunch()


def _build_web_ui(web_dir: Path, *, fatal: bool = False) -> bool:
    """Serialize dashboard rebuilds, checking freshness only after acquiring the lock."""
    from hermes_cli.runtime_state import _lock

    if not (web_dir / "package.json").exists():
        return True
    try:
        with (_web_project_root(web_dir) / ".web_ui_build.lock").open("ab") as lock_file:
            _lock(lock_file.fileno(), wait=True)
            return _do_build_web_ui(web_dir, fatal=fatal)
    except OSError as exc:
        _console_print(f"  ✗ Could not lock the web UI build: {exc}")
        return False


def _do_build_web_ui(web_dir: Path, *, fatal: bool = False) -> bool:
    """Build stale dashboard sources; failure is never reported as a usable build."""
    from hermes_cli.source_build import build_source_web, prepare_launch_dependencies, source_build_env

    if not (web_dir / "package.json").exists() or not _web_ui_build_needed(web_dir):
        return True
    project_root = _web_project_root(web_dir)
    _console_print("→ Building web UI...")
    try:
        env = source_build_env()
        prepare_launch_dependencies(project_root, env=env)
        build_source_web(project_root, env=env)
    except (OSError, subprocess.SubprocessError, RuntimeError) as exc:
        _console_print(f"  {'✗' if fatal else '⚠'} Web UI build failed: {exc}")
        return False
    _console_print("  ✓ Web UI built")
    return True
