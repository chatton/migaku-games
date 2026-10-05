"""Logging, subprocess and locking helpers shared by the host modules.

Everything the host side does is logged to a rotating file (log_file()), because the hotkey runs
it from a desktop shortcut where stdout/stderr go nowhere. Each run logs the environment it saw,
the backends it chose, every command with its exit code, output and timing, every server call,
and full tracebacks on failure.
"""
import contextlib
import logging
import logging.handlers
import os
import shlex
import subprocess
import sys
import tempfile
import time
from datetime import datetime
from pathlib import Path

log = logging.getLogger("migaku")


class AppError(Exception):
    """A failure to report to the user (desktop notification + log)."""


def state_dir() -> Path:
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Logs" / "migaku-games"
    if os.name == "nt":
        return Path(os.environ.get("LOCALAPPDATA") or Path.home()) / "migaku-games"
    return Path(os.environ.get("XDG_STATE_HOME") or Path.home() / ".local" / "state") / "migaku-games"


def runtime_dir() -> Path:
    """Short-lived state (the frozen-process list, the run lock); cleared on reboot where possible."""
    base = os.environ.get("XDG_RUNTIME_DIR") or tempfile.gettempdir()
    path = Path(base) / "migaku-games"
    path.mkdir(parents=True, exist_ok=True)
    return path


def log_file() -> Path:
    return state_dir() / "host.log"


class IsoFormatter(logging.Formatter):
    """ISO 8601 timestamps with milliseconds and UTC offset, so host and server logs line up."""

    def formatTime(self, record, datefmt=None):
        return datetime.fromtimestamp(record.created).astimezone().isoformat(timespec="milliseconds")


def setup_logging(verbose: bool = False) -> None:
    """File: everything (DEBUG). Console: INFO in a terminal (DEBUG with --verbose), else warnings."""
    log.setLevel(logging.DEBUG)
    fmt = IsoFormatter("%(asctime)s [%(process)d] %(levelname)-7s %(message)s")
    try:
        log_file().parent.mkdir(parents=True, exist_ok=True)
        fh = logging.handlers.RotatingFileHandler(log_file(), maxBytes=2_000_000, backupCount=3, encoding="utf-8")
        fh.setFormatter(fmt)
        fh.setLevel(logging.DEBUG)
        log.addHandler(fh)
    except OSError as e:  # still usable without a log file
        print(f"migaku-games: can't write {log_file()}: {e}", file=sys.stderr)
    console = logging.StreamHandler(sys.stderr)
    console.setFormatter(logging.Formatter("%(levelname)s %(message)s"))
    console.setLevel(logging.DEBUG if verbose else logging.INFO if sys.stderr and sys.stderr.isatty() else logging.WARNING)
    log.addHandler(console)


@contextlib.contextmanager
def step(name: str):
    """Log a named step with its duration; failures are logged with the time they took."""
    start = time.monotonic()
    log.debug("-> %s", name)
    try:
        yield
    except BaseException as e:
        log.warning("x  %s failed after %d ms: %s", name, (time.monotonic() - start) * 1000, e)
        raise
    log.info("ok %s (%d ms)", name, (time.monotonic() - start) * 1000)


def _clip(text, limit=2000) -> str:
    text = (text or "").strip()
    return text if len(text) <= limit else text[:limit] + f"... [{len(text) - limit} more chars]"


def run(cmd: list, check: bool = True, timeout: float = 60, **kwargs) -> subprocess.CompletedProcess:
    """Run a command, logging it with its exit code, output and duration. A missing program or a
    failure (with check) raises AppError naming the command."""
    shown = " ".join(shlex.quote(str(c)) for c in cmd)
    if len(shown) > 400:  # e.g. inline scripts
        shown = shown[:400] + "..."
    start = time.monotonic()
    try:
        result = subprocess.run([str(c) for c in cmd], capture_output=True, text=True, timeout=timeout, **kwargs)
    except FileNotFoundError:
        log.error("command not found: %s", cmd[0])
        raise AppError(f"{cmd[0]} is not installed (needed for: {shown[:80]})") from None
    except subprocess.TimeoutExpired:
        log.error("command timed out after %ss: %s", timeout, shown)
        raise AppError(f"{cmd[0]} timed out after {timeout}s") from None
    ms = (time.monotonic() - start) * 1000
    level = logging.DEBUG if result.returncode == 0 else logging.WARNING
    log.log(level, "$ %s -> exit %d (%d ms)", shown, result.returncode, ms)
    if result.stdout and result.stdout.strip():
        log.log(level, "  stdout: %s", _clip(result.stdout))
    if result.stderr and result.stderr.strip():
        log.log(level, "  stderr: %s", _clip(result.stderr))
    if check and result.returncode != 0:
        raise AppError(f"{cmd[0]} failed (exit {result.returncode}): {_clip(result.stderr or result.stdout, 200)}")
    return result


@contextlib.contextmanager
def single_instance(name: str = "hotkey"):
    """Yield True if this process got the lock, False if another run holds it (a double press)."""
    path = runtime_dir() / f"{name}.lock"
    handle = open(path, "a+")
    try:
        if os.name == "nt":
            import msvcrt
            try:
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            except OSError:
                yield False
                return
        else:
            import fcntl
            try:
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                yield False
                return
        yield True
    finally:
        handle.close()  # releases the lock
