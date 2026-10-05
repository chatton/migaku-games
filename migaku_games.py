"""Capture client: screenshot -> frame server (OCR) -> viewer in Brave for Migaku.

    python3 migaku_games.py                 # drag-select a screen region
    python3 migaku_games.py --full          # whole screen
    python3 migaku_games.py --image x.png   # skip capture, use an existing image
    python3 migaku_games.py --app           # open as a chromeless Brave app window
    python3 migaku_games.py --ocr meiki     # ask the server for a specific OCR engine

Capture, clipboard and the browser are host-side; OCR and storage happen in the frame
server container (compose.yaml). If nothing is listening at the default address, the
container is started with `docker compose up -d` (or podman).
"""
import argparse
import json
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent
DEFAULT_SERVER = "http://localhost:8765"
BROWSER = "Brave Browser"
MAC = sys.platform == "darwin"


def capture(dest: Path, full: bool) -> None:
    if MAC:
        cmd = ["screencapture", "-x"] if full else ["screencapture", "-x", "-i"]
    else:
        # KDE Plasma (Bazzite desktop mode): -b background, -n no notification.
        cmd = ["spectacle", "-b", "-n", "-f" if full else "-r", "-o"]
    subprocess.run(cmd + [str(dest)], check=True)
    if not dest.exists() or not dest.stat().st_size:
        sys.exit("capture cancelled")


def copy_image_to_clipboard(image: Path) -> None:
    """So the frame can be pasted into the Migaku card's image field."""
    if MAC:
        script = f'set the clipboard to (read (POSIX file "{image}") as «class PNGf»)'
        subprocess.run(["osascript", "-e", script], check=True)
    elif shutil.which("wl-copy"):
        with image.open("rb") as f:
            subprocess.run(["wl-copy", "--type", "image/png"], stdin=f, check=True)


def server_up(server: str) -> bool:
    try:
        urllib.request.urlopen(server + "/api/config", timeout=2).close()
        return True
    except (urllib.error.URLError, OSError):
        return False


def compose_command() -> list:
    for cmd in (["docker", "compose"], ["podman", "compose"]):
        if shutil.which(cmd[0]):
            return cmd
    sys.exit("neither docker nor podman found; start the frame server yourself")


def start_container(server: str) -> None:
    """`docker compose up -d` in the project (builds the image on first run), then wait for it."""
    print("starting the frame server container ...")
    subprocess.run(compose_command() + ["up", "-d"], cwd=ROOT, check=True)
    for _ in range(100):
        if server_up(server):
            return
        time.sleep(0.3)
    sys.exit("frame server container did not come up; see `docker compose logs`")


def upload(server: str, image: Path, engine) -> dict:
    url = server + "/api/frames" + (f"?ocr={engine}" if engine else "")
    req = urllib.request.Request(url, data=image.read_bytes(), method="POST",
                                 headers={"Content-Type": "application/octet-stream"})
    try:
        with urllib.request.urlopen(req, timeout=120) as resp:
            return json.load(resp)
    except urllib.error.HTTPError as e:
        sys.exit(f"server error: {json.load(e).get('error', e)}")


def brave_command() -> list:
    """Brave on Linux: a native package, or the Flatpak (Bazzite's usual install)."""
    for exe in ("brave-browser", "brave"):
        if shutil.which(exe):
            return [exe]
    flatpak = subprocess.run(["flatpak", "info", "com.brave.Browser"], capture_output=True) if shutil.which("flatpak") else None
    if flatpak and flatpak.returncode == 0:
        return ["flatpak", "run", "com.brave.Browser"]
    sys.exit("Brave not found (looked for brave-browser, brave and the com.brave.Browser Flatpak)")


def open_browser(url: str, app: bool) -> None:
    if MAC:
        if app:
            subprocess.run(["open", "-na", BROWSER, "--args", f"--app={url}"], check=True)
        else:
            subprocess.run(["open", "-a", BROWSER, url], check=True)
        return
    # Detached so the browser outlives this command.
    subprocess.Popen(brave_command() + ([f"--app={url}"] if app else [url]),
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--image", type=Path, help="use this image instead of taking a screenshot")
    p.add_argument("--full", action="store_true", help="capture the whole screen instead of a region")
    p.add_argument("--app", action="store_true", help="open in a chromeless Brave app window")
    p.add_argument("--ocr", choices=["vision", "meiki"], help="OCR engine (default: the server's; the container has meiki only)")
    p.add_argument("--server", default=DEFAULT_SERVER, help=f"frame server (default {DEFAULT_SERVER})")
    p.add_argument("--no-open", action="store_true", help="don't open the viewer")
    args = p.parse_args()
    server = args.server.rstrip("/")

    if not server_up(server):
        if server != DEFAULT_SERVER:
            sys.exit(f"no frame server at {server}")
        start_container(server)

    with tempfile.TemporaryDirectory() as tmp:
        shot = Path(tmp) / "shot.png"
        if args.image:
            shot = args.image
        else:
            capture(shot, args.full)
        if MAC and shot.suffix.lower() != ".png":
            # The clipboard copy needs PNG data; the server normalises its own copy.
            png = Path(tmp) / "clip.png"
            subprocess.run(["sips", "-s", "format", "png", str(shot), "--out", str(png)],
                           check=True, capture_output=True)
            copy_image_to_clipboard(png)
        else:
            copy_image_to_clipboard(shot)
        frame = upload(server, shot, args.ocr)

    for line in frame["lines"] or ["(no text found)"]:
        print(f"  {line}")
    if not args.no_open:
        open_browser(server + frame["url"], args.app)


if __name__ == "__main__":
    main()
