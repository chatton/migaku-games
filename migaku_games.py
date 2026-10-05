"""Capture client: screenshot -> frame server (OCR) -> viewer in Brave for Migaku.

    python3 migaku_games.py                 # drag-select a screen region
    python3 migaku_games.py --full          # whole screen
    python3 migaku_games.py --image x.png   # skip capture, use an existing image
    python3 migaku_games.py --app           # open as a chromeless Brave app window
    python3 migaku_games.py --ocr meiki     # ask the server for a specific OCR engine
    python3 migaku_games.py --overlay       # bind to a hotkey: capture the screen and show it in
                                            # one long-lived Brave window over the game; the same
                                            # key hides that window again

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
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent
DEFAULT_SERVER = "http://localhost:8765"
BROWSER = "Brave Browser"
MAC = sys.platform == "darwin"


def capture(dest: Path, full: bool, screen: bool = False) -> None:
    if MAC:
        cmd = ["screencapture", "-x"] + (["-m"] if screen else [] if full else ["-i"])
    else:
        # KDE Plasma (Bazzite desktop mode): -b background, -n no notification,
        # -m the screen under the mouse, -f all screens, -r region.
        cmd = ["spectacle", "-b", "-n", "-m" if screen else "-f" if full else "-r", "-o"]
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


def upload(server: str, image: Path, engine, game=None, wait=True) -> dict:
    query = urllib.parse.urlencode({k: v for k, v in (("ocr", engine), ("game", game), ("wait", "" if wait else "0")) if v})
    url = server + "/api/frames" + (f"?{query}" if query else "")
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


# --- overlay mode: one long-lived Brave window ------------------------------------------
LIVE_TITLE = "Migaku Live"  # the live viewer's page title, which names its window

KWIN_SCRIPT = """
const wins = workspace.windowList ? workspace.windowList() : workspace.clientList();  // Plasma 6 : 5
for (const w of wins) {
  if (!w.caption.includes("%s")) continue;
  if ("%s" === "show") {
    w.minimized = false;
    w.fullScreen = true;
    if (workspace.windowList) workspace.activeWindow = w; else workspace.activeClient = w;
  } else {
    w.minimized = true;
  }
}
"""


def kwin(action: str) -> None:
    """Show or hide the live window via a one-off KWin script (dbus-send ships with Plasma)."""
    def call(method, *args):
        return subprocess.run(["dbus-send", "--session", "--print-reply", "--dest=org.kde.KWin", "/Scripting",
                               f"org.kde.kwin.Scripting.{method}", *args], capture_output=True, text=True)
    with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False) as f:
        f.write(KWIN_SCRIPT % (LIVE_TITLE, action))
    try:
        call("unloadScript", "string:migaku-live")
        loaded = call("loadScript", f"string:{f.name}", "string:migaku-live")
        if loaded.returncode:
            sys.exit(f"KWin scripting failed: {loaded.stderr.strip()}")
        call("start")
        call("unloadScript", "string:migaku-live")
    finally:
        Path(f.name).unlink(missing_ok=True)


MAC_SCRIPT = """
ObjC.import("AppKit");
const brave = Application("%s");
const w = brave.windows().find((w) => w.name().includes("%s"));
if (w && "%s" === "show") {
  const f = $.NSScreen.mainScreen.frame;
  w.minimized = false;
  w.bounds = { x: 0, y: 0, width: f.size.width, height: f.size.height };
  w.index = 1;
  brave.activate();
} else if (w) {
  w.minimized = true;
}
"""


def mac_window(action: str) -> None:
    """macOS asks once to let the terminal control Brave (Privacy & Security > Automation)."""
    r = subprocess.run(["osascript", "-l", "JavaScript", "-e", MAC_SCRIPT % (BROWSER, LIVE_TITLE, action)],
                       capture_output=True, text=True)
    if r.returncode:
        sys.exit(f"couldn't {action} the live window: {r.stderr.strip()}")


def live_window(action: str) -> None:
    (mac_window if MAC else kwin)(action)


def live_state(server: str) -> dict:
    with urllib.request.urlopen(server + "/api/live", timeout=2) as resp:
        return json.load(resp)


def wait_live(server: str, until, timeout: float) -> bool:
    end = time.time() + timeout
    while time.time() < end:
        if until(live_state(server)):
            return True
        time.sleep(0.1)
    return False


def overlay(server: str, engine, game) -> None:
    """The hotkey: hide the live window if it has focus, else capture the screen and show it."""
    state = live_state(server)
    if state["focused"]:
        live_window("hide")
        return
    with tempfile.TemporaryDirectory() as tmp:
        shot = Path(tmp) / "shot.png"
        capture(shot, full=False, screen=True)
        copy_image_to_clipboard(shot)
        frame = upload(server, shot, engine, game, wait=False)  # OCR carries on in the background
    if not state["open"]:
        open_browser(server + "/viewer.html?live", app=True)
        if not wait_live(server, lambda s: s["open"], 15):
            sys.exit("the live window didn't open; is Brave running?")
    # Raise it once it shows the new picture, so the old one never flashes up.
    wait_live(server, lambda s: s["frame"] == frame["id"], 2)
    live_window("show")


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--image", type=Path, help="use this image instead of taking a screenshot")
    p.add_argument("--full", action="store_true", help="capture the whole screen instead of a region")
    p.add_argument("--app", action="store_true", help="open in a chromeless Brave app window")
    p.add_argument("--ocr", choices=["vision", "meiki"], help="OCR engine (default: the server's; the container has meiki only)")
    p.add_argument("--game", help="tag the frame with this game name")
    p.add_argument("--server", default=DEFAULT_SERVER, help=f"frame server (default {DEFAULT_SERVER})")
    p.add_argument("--no-open", action="store_true", help="don't open the viewer")
    p.add_argument("--overlay", action="store_true",
                   help="capture the screen into the long-lived live window, or hide it if it has focus")
    args = p.parse_args()
    server = args.server.rstrip("/")

    if not server_up(server):
        if server != DEFAULT_SERVER:
            sys.exit(f"no frame server at {server}")
        start_container(server)
    if args.overlay:
        return overlay(server, args.ocr, args.game)

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
        frame = upload(server, shot, args.ocr, args.game)

    for line in frame["lines"] or ["(no text found)"]:
        print(f"  {line}")
    if not args.no_open:
        open_browser(server + frame["url"], args.app)


if __name__ == "__main__":
    main()
