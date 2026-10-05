"""Steam screenshot watcher: every new Steam screenshot -> frame server (OCR), tagged with the game.

    python3 steam_watcher.py                # watch every Steam account's screenshot folders
    python3 steam_watcher.py --open         # also open each new frame in Brave
    python3 steam_watcher.py --dir ~/shots  # watch a plain folder instead (any screenshot tool)
    python3 steam_watcher.py --steam /steam --server http://migaku-games:8765   # in compose

Steam's screenshot key (F12, or a controller button mapped in Steam Input) works in Bazzite's
Game Mode, where desktop capture tools can't see the game. Steam saves each shot to
<Steam>/userdata/<account>/760/remote/<appid>/screenshots/; the app id names the game.

Files already there when the watcher starts are skipped; only new ones are uploaded. Stdlib
only, polling (no inotify), so it behaves the same for native and Flatpak Steam.
"""
import argparse
import re
import sys
import time
from pathlib import Path

from migaku_games import DEFAULT_SERVER, ENGINES, AppError, ensure_server, open_browser, upload

HOME = Path.home()
STEAM_ROOTS = [
    HOME / ".local/share/Steam",
    HOME / ".steam/steam",
    HOME / ".var/app/com.valvesoftware.Steam/.local/share/Steam",  # Flatpak
    HOME / "Library/Application Support/Steam",                     # macOS
]
IMAGE_SUFFIXES = {".jpg", ".jpeg", ".png"}
POLL = 1.0  # seconds


def steam_roots(candidates: list) -> list:
    """Existing Steam installs, deduplicated (~/.steam/steam is usually a symlink)."""
    seen, roots = set(), []
    for r in candidates:
        if (r / "userdata").is_dir() and r.resolve() not in seen:
            seen.add(r.resolve())
            roots.append(r)
    return roots


def game_names(roots: list) -> dict:
    """app id -> name, from the appmanifest_<id>.acf files in every Steam library."""
    libraries = []
    for root in roots:
        libraries.append(root / "steamapps")
        try:
            vdf = (root / "steamapps/libraryfolders.vdf").read_text(errors="replace")
            libraries += [Path(p.replace("\\\\", "\\")) / "steamapps" for p in re.findall(r'"path"\s+"([^"]+)"', vdf)]
        except OSError:
            pass
    names = {}
    for lib in libraries:
        for manifest in lib.glob("appmanifest_*.acf"):
            try:
                text = manifest.read_text(errors="replace")
            except OSError:
                continue
            appid = re.search(r'"appid"\s+"(\d+)"', text)
            name = re.search(r'"name"\s+"([^"]+)"', text)
            if appid and name:
                names[appid.group(1)] = name.group(1)
    return names


def scan(roots: list, dirs: list) -> dict:
    """Every screenshot file -> its app id (None for plain --dir folders)."""
    found = {}
    for root in roots:
        # Only the screenshots themselves, not screenshots/thumbnails/.
        for f in root.glob("userdata/*/760/remote/*/screenshots/*"):
            if f.suffix.lower() in IMAGE_SUFFIXES:
                found[f] = f.parent.parent.name
    for d in dirs:
        for f in d.iterdir():
            if f.suffix.lower() in IMAGE_SUFFIXES:
                found[f] = None
    return found


def settled(path: Path, sizes: dict) -> bool:
    """True once the file's size has stopped changing between polls (Steam writes in chunks)."""
    try:
        size = path.stat().st_size
    except OSError:
        return False
    prev = sizes.get(path)
    sizes[path] = size
    return size > 0 and size == prev


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--dir", type=Path, action="append", default=[],
                   help="watch this folder instead of Steam's (repeatable)")
    p.add_argument("--steam", type=Path, action="append", default=[],
                   help="Steam install folder (repeatable; default: the usual native/Flatpak/macOS paths)")
    p.add_argument("--game", help="game tag for frames from --dir folders")
    p.add_argument("--open", action="store_true", help="open each new frame in Brave (host only, not in compose)")
    p.add_argument("--app", action="store_true", help="with --open: chromeless Brave app window")
    p.add_argument("--ocr", choices=ENGINES, help="OCR engine (default: the server's)")
    p.add_argument("--server", default=DEFAULT_SERVER, help=f"frame server (default {DEFAULT_SERVER})")
    args = p.parse_args()
    server = args.server.rstrip("/")

    dirs = [d.expanduser() for d in args.dir]
    for d in dirs:
        if not d.is_dir():
            sys.exit(f"not a folder: {d}")
    candidates = args.steam or STEAM_ROOTS
    roots = [] if dirs else steam_roots(candidates)
    if not dirs and not roots:
        sys.exit("no Steam install found (looked in " + ", ".join(str(r) for r in candidates) + "); use --steam or --dir")

    try:
        ensure_server(server)
    except AppError as e:
        sys.exit(str(e))

    names = game_names(roots)
    unnamed = set()  # app ids with no manifest (non-Steam shortcuts): don't re-read them all each time
    seen = set(scan(roots, dirs))
    sizes = {}
    print("watching " + ", ".join(str(x) for x in (dirs or roots)) + f"  ({len(seen)} existing screenshots skipped)", flush=True)

    while True:
        time.sleep(POLL)
        try:
            current = scan(roots, dirs)
        except OSError as e:  # a folder vanished mid-scan; try again next poll
            print(f"scan: {e}", file=sys.stderr, flush=True)
            continue
        for f in sizes.keys() - current.keys():  # gone before it finished writing
            del sizes[f]
        for f, appid in current.items():
            if f in seen or not settled(f, sizes):
                continue
            del sizes[f]
            seen.add(f)
            if appid and appid not in names and appid not in unnamed:
                names.update(game_names(roots))  # a game installed since the watcher started
                if appid not in names:
                    unnamed.add(appid)
            game = args.game if appid is None else names.get(appid, f"app {appid}")
            try:
                frame = upload(server, f, args.ocr, game)
            except AppError as e:
                print(f"{f.name}: {e}", file=sys.stderr, flush=True)
                continue
            except OSError as e:
                print(f"{f.name}: server unreachable ({e})", file=sys.stderr, flush=True)
                continue
            lines = frame["lines"]
            print(f"{game or f.name}: {len(lines)} line(s)  {lines[0] if lines else '(no text found)'}", flush=True)
            if args.open:
                open_browser(server + frame["url"], args.app)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        pass
