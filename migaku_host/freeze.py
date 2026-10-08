"""Experimental: pause the game's processes while the overlay is up.

Linux/macOS: SIGSTOP/SIGCONT. Windows: NtSuspendProcess/NtResumeProcess. The game is the process
whose executable name contains the profile's `process` text; on Linux with none set, the running
Steam game (Steam starts every game, Proton or native, under `reaper SteamLaunch AppId=N`).
What was frozen is written down first, so any later press (or --resume) can undo it."""
import json
import os
import re
import signal
import sys

from .util import AppError, log, run, runtime_dir


def frozen_file():
    return runtime_dir() / "frozen.json"


def processes() -> dict:
    """pid -> (parent pid, argv list)."""
    if sys.platform.startswith("linux"):
        return _linux_processes()
    if os.name == "nt":
        return _windows_processes()
    return _ps_processes()


def _linux_processes() -> dict:
    from pathlib import Path
    procs = {}
    for d in Path("/proc").iterdir():
        if not d.name.isdigit():
            continue
        try:
            ppid = int((d / "stat").read_text().rsplit(")", 1)[1].split()[1])
            argv = (d / "cmdline").read_bytes().decode(errors="replace").split("\0")
        except (OSError, ValueError, IndexError):
            continue
        procs[int(d.name)] = (ppid, [a for a in argv if a])
    return procs


def _ps_processes() -> dict:
    procs = {}
    for line in run(["ps", "-axo", "pid=,ppid=,comm="]).stdout.splitlines():
        parts = line.split(None, 2)
        if len(parts) == 3 and parts[0].isdigit():
            procs[int(parts[0])] = (int(parts[1]), [parts[2].strip()])
    return procs


def _windows_processes() -> dict:
    import ctypes
    from ctypes import wintypes

    class Entry(ctypes.Structure):
        _fields_ = [("dwSize", wintypes.DWORD), ("cntUsage", wintypes.DWORD), ("th32ProcessID", wintypes.DWORD),
                    ("th32DefaultHeapID", ctypes.c_void_p), ("th32ModuleID", wintypes.DWORD),
                    ("cntThreads", wintypes.DWORD), ("th32ParentProcessID", wintypes.DWORD),
                    ("pcPriClassBase", ctypes.c_long), ("dwFlags", wintypes.DWORD), ("szExeFile", ctypes.c_wchar * 260)]

    k32 = ctypes.windll.kernel32
    snap = k32.CreateToolhelp32Snapshot(0x2, 0)  # TH32CS_SNAPPROCESS
    entry, procs = Entry(), {}
    entry.dwSize = ctypes.sizeof(Entry)
    ok = k32.Process32FirstW(snap, ctypes.byref(entry))
    while ok:
        procs[entry.th32ProcessID] = (entry.th32ParentProcessID, [entry.szExeFile])
        ok = k32.Process32NextW(snap, ctypes.byref(entry))
    k32.CloseHandle(snap)
    return procs


def game_process(pattern: str, procs: dict):
    pattern = pattern.strip().lower()
    mine = {os.getpid(), os.getppid()}
    for pid, (_, argv) in procs.items():
        if not argv or pid in mine:
            continue
        if pattern:
            # Executable name only (Wine paths use backslashes), so an argument can't match.
            if pattern in re.split(r"[/\\]", argv[0])[-1].lower():
                return pid
        elif argv[0].endswith("reaper") and "SteamLaunch" in argv:
            return pid
    return None


def _environ(pid: int) -> dict:
    try:
        raw = open(f"/proc/{pid}/environ", "rb").read().decode(errors="replace")
    except OSError:
        return {}
    return dict(item.split("=", 1) for item in raw.split("\0") if "=" in item)


def steam_app_id(argv: list):
    """The AppId=N argument Steam gives `reaper` for the game it launches."""
    return next((a.split("=", 1)[1] for a in argv if a.startswith("AppId=")), None)


def steam_game_tree(root: int, procs: dict, environ=_environ) -> list:
    """The reaper's own tree plus every process carrying the game's SteamAppId. Flatpak Steam
    starts the game's container through flatpak-portal, outside the reaper's tree, so the game
    itself is only found by that variable."""
    tree = process_tree(root, procs)
    app_id = steam_app_id(procs[root][1])
    if app_id:
        mine = {os.getpid(), os.getppid()}
        for pid in procs:
            if pid not in tree and pid not in mine and environ(pid).get("SteamAppId") == app_id:
                tree += [p for p in process_tree(pid, procs) if p not in tree]
    return tree


# Steam's own runtimes, which live under steamapps/common like the games do.
_STEAM_RUNTIMES = ("proton", "steamlinuxruntime", "steamworks")


def game_executables(tree: list, procs: dict) -> list:
    """The game's own processes within a Steam launch: executables under steamapps/common/<game>.
    Wine's services and steam.exe (Steam's stand-in, which the Steam client talks to), Proton and
    the runtime container stay running, or the Steam client hangs while the game is frozen.
    Falls back to the whole tree if nothing matches."""
    games = []
    for pid in tree:
        exe = procs[pid][1][0] if procs[pid][1] else ""
        parts = [p.lower() for p in re.split(r"[/\\]", exe)]
        if "common" in parts and parts.index("common") > 0 and parts[parts.index("common") - 1] == "steamapps":
            title = parts[parts.index("common") + 1] if len(parts) > parts.index("common") + 1 else ""
            if title and not title.startswith(_STEAM_RUNTIMES):
                games.append(pid)
    return games or tree


def process_tree(root: int, procs: dict) -> list:
    children = {}
    for pid, (ppid, _) in procs.items():
        children.setdefault(ppid, []).append(pid)
    tree, todo = [], [root]
    while todo:
        pid = todo.pop()
        tree.append(pid)
        todo += children.get(pid, [])
    return tree


def _signal_all(pids, suspend: bool) -> None:
    if os.name == "nt":
        import ctypes
        ntdll, k32 = ctypes.windll.ntdll, ctypes.windll.kernel32
        call = ntdll.NtSuspendProcess if suspend else ntdll.NtResumeProcess
        for pid in pids:
            handle = k32.OpenProcess(0x0800, False, pid)  # PROCESS_SUSPEND_RESUME
            if handle:
                call(handle)
                k32.CloseHandle(handle)
            else:
                log.warning("freeze: can't open process %d", pid)
        return
    sig = signal.SIGSTOP if suspend else signal.SIGCONT
    for pid in pids:
        try:
            os.kill(pid, sig)
        except ProcessLookupError:
            log.debug("freeze: process %d is gone", pid)
        except PermissionError:
            log.warning("freeze: no permission to signal process %d", pid)


def freeze_game(profile: dict) -> None:
    if not profile.get("freeze"):
        return
    pattern = profile.get("process", "")
    if not pattern and not sys.platform.startswith("linux"):
        raise AppError(f"freezing {profile.get('name')}: set its process name in the config (Steam detection is Linux-only)")
    procs = processes()
    root = game_process(pattern, procs)
    if not root:
        what = f"no running process matches {pattern!r}" if pattern else "no running Steam game found"
        raise AppError(f"freezing {profile.get('name')}: {what}")
    tree = game_executables(steam_game_tree(root, procs), procs) if not pattern else process_tree(root, procs)
    log.info("freeze: %s root %d %s, %d processes: %s", profile.get("name"), root, procs[root][1][:3], len(tree), tree)
    frozen_file().write_text(json.dumps(tree))  # recorded first, so it can always be undone
    _signal_all(tree, suspend=True)


def resume() -> None:
    try:
        pids = json.loads(frozen_file().read_text())
    except (OSError, ValueError):
        return
    log.info("freeze: resuming %d processes", len(pids))
    _signal_all(reversed(pids), suspend=False)
    frozen_file().unlink(missing_ok=True)
