"""Capture client: screenshot -> frame server (OCR) -> the viewer in the browser, for Migaku.

    python3 migaku_games.py --overlay       # the hotkey: capture the screen and show it in one
                                            # long-lived browser window over the game; pressed
                                            # again while it's shown, hides it
    python3 migaku_games.py --hide          # just hide the overlay (and resume a frozen game)
    python3 migaku_games.py                 # drag-select a region, open it in a new tab
    python3 migaku_games.py --full          # whole screen
    python3 migaku_games.py --image x.png   # an existing image
    python3 migaku_games.py --doctor        # what this machine has, what's missing, where logs are
    python3 migaku_games.py --resume        # unfreeze a game left frozen (experimental freezing)

Works on Linux (KDE, GNOME, Sway, Hyprland, X11), macOS and Windows: the platform-specific parts
(capture, the overlay window, notifications, freezing) live in migaku_host/, with backends picked
per desktop or set in the config's `host` section. Every run is logged in detail (see --doctor
for the file); failures also show a desktop notification.
"""
import argparse
import platform
import subprocess
import sys
import tempfile
import time
import traceback
from pathlib import Path

from migaku_host import browser, capture, desktop, freeze, window
from migaku_host.notify import notify
from migaku_host.server_api import DEFAULT_SERVER, ROOT, ensure_server, native_service, request, server_up, upload, wait_live
from migaku_host.util import AppError, log, log_file, setup_logging, single_instance, step

ENGINES = ("vision", "meiki")  # the server's pipeline.ENGINES; the container has meiki only


def version() -> str:
    try:
        return subprocess.run(["git", "-C", str(ROOT), "describe", "--always", "--dirty", "--tags"],
                              capture_output=True, text=True, timeout=5).stdout.strip() or "unknown"
    except (OSError, subprocess.SubprocessError):
        return "unknown"


class Session:
    """One run: the detected desktop, the config from the server and the chosen backends."""

    def __init__(self, server: str):
        self.server = server
        self.desktop = desktop.detect()
        self.config = {}
        self.host = {}

    def load_config(self):
        self.config = request(self.server, "/api/config")
        if self.config.get("error"):
            log.warning("server config has an error (using the previous one): %s", self.config["error"])
        self.host = self.config.get("host", {})
        self.capture = capture.choose(self.desktop, self.host.get("capture", "auto"))
        self.window = window.choose(self.desktop, self.host.get("window", "auto"))
        log.info("backends: capture=%s window=%s%s", self.capture.name, self.window.name,
                 "" if self.window.can_raise else f" (follow mode{': ' + window.describe_unsupported(self.desktop) if window.describe_unsupported(self.desktop) else ''})")

    def notify(self, title, body="", error=False):
        if self.host.get("notifications", True) or error:
            notify(self.desktop, title, body, error)

    @property
    def profile(self) -> dict:
        return self.config.get("profiles", {}).get(self.config.get("active_profile") or "", {})


def overlay(s: Session, engine, game) -> None:
    """The hotkey: hide the overlay if it's shown, else capture the screen and show it."""
    was_frozen = freeze.frozen_file().exists()
    freeze.resume()  # a game left frozen (overlay closed some other way) comes back first
    if was_frozen and not s.window.can_raise:
        # Follow mode can't tell whether the overlay is up, so a press that finds the game
        # paused by the last one is the "back to the game" press: resume only.
        log.info("follow mode: the game was paused, so this press only resumes it")
        return
    state = request(s.server, "/api/live", timeout=2)
    log.info("live window: %s", state)
    if state.get("shown"):
        return hide(s)
    try:
        s.window.note_focus()  # before the capture: the screenshot portal takes focus briefly
    except AppError as e:
        log.warning("window: couldn't note the focused window: %s", e)
    with tempfile.TemporaryDirectory() as tmp:
        shot = Path(tmp) / "shot.png"
        with step("capture"):
            capture.capture(s.capture, shot, "screen")
        if s.host.get("clipboard", True):
            capture.copy_to_clipboard(s.desktop, shot)
        if s.profile.get("freeze"):
            with step("freeze"):
                try:
                    freeze.freeze_game(s.profile)
                except AppError as e:  # the overlay is still useful without it
                    s.notify("Couldn't freeze the game", str(e), error=True)
        try:
            with step("upload"):
                frame = upload(s.server, shot, engine, game or s.profile.get("name"), wait=False)
            def open_live():
                with step("open live window"):
                    # On GNOME (and in follow mode) the live window gets its own browser
                    # instance and named profile, as a normal maximised window.
                    profile = s.host.get("overlay_profile", True)
                    own = profile is not False and s.window.name in ("gnome", "follow")
                    browser.open_url(s.desktop, s.server + "/viewer.html?live", app=not own,
                                     configured=s.host.get("browser", ""), overlay=own, profile=profile)
                    if not wait_live(s.server, lambda st: st["open"], 30):
                        raise AppError("the live window didn't open within 30s; is the browser running? "
                                       "(first time: open it once from a terminal and authorise Migaku)")

            def show_live():
                # Raise it once it shows the new picture, so the old one never flashes up.
                with step("wait for the picture"):
                    if not wait_live(s.server, lambda st: st["frame"] == frame["id"], 3):
                        log.warning("the live window hasn't shown frame %s yet; raising anyway", frame["id"])
                with step(f"show window ({s.window.name})"):
                    s.window.show()

            if not state.get("open"):
                open_live()
            try:
                show_live()
            except AppError as e:
                # The server still counts a just-closed window as open for a few seconds.
                if "no Migaku Live window" not in str(e):
                    raise
                log.info("the live window is gone; opening a new one")
                open_live()
                show_live()
            if s.window.can_raise:
                request(s.server, "/api/live", data={"shown": True})
            else:
                s.notify("Frame captured", "Switch to the Migaku Live window.")
        except Exception:
            freeze.resume()  # never leave the game frozen behind an overlay that didn't appear
            raise


def hide(s: Session) -> None:
    with step(f"hide window ({s.window.name})"):
        s.window.hide()
    request(s.server, "/api/live", data={"shown": False})
    freeze.resume()


def capture_once(s: Session, args) -> None:
    with tempfile.TemporaryDirectory() as tmp:
        shot = args.image or Path(tmp) / "shot.png"
        if not args.image:
            with step("capture"):
                capture.capture(s.capture, shot, "full" if args.full else "region")
        with step("upload + OCR"):
            frame = upload(s.server, shot, args.ocr, args.game)
    for line in frame["lines"] or ["(no text found)"]:
        print(f"  {line}")
    if not args.no_open:
        browser.open_url(s.desktop, s.server + frame["url"], args.app, s.host.get("browser", ""))


def doctor(server: str) -> int:
    """Print what this machine has and what's missing; also written to the log."""
    d = desktop.detect()
    out = []
    say = out.append
    say(f"migaku-games {version()}  python {platform.python_version()}  {d.os_release}")
    say(f"desktop: os={d.os} session={d.session} desktop={d.desktop}")
    for k, v in desktop.environment().items():
        say(f"  {k}={v if k != 'PATH' else v[:200]}")
    say("capture backends:")
    for b in capture.BACKENDS:
        say(f"  {'*' if b.suits(d) else ' '} {b.name:17} {'ok' if not b.missing() else 'missing ' + ', '.join(b.missing())}")
    say("window backends:")
    for b in window.BACKENDS:
        say(f"  {'*' if b.suits(d) else ' '} {b.name:17} {'ok' if not b.missing() else 'missing ' + ', '.join(b.missing())}")
    say("  (* = suits this desktop)")
    try:
        say(f"browser: {browser.browser_command(d)}")
    except AppError as e:
        say(f"browser: {e}")
    native = native_service()
    say(f"frame server deployment: {'native (' + ' '.join(native) + ')' if native else 'container (docker/podman compose)'}")
    for tool in ("docker", "podman", "notify-send", "git"):
        say(f"tool {tool}: {desktop.which(tool) or 'not found'}")
    ok = server_up(server)
    say(f"frame server {server}: {'up' if ok else 'NOT reachable'}")
    if ok:
        s = Session(server)
        try:
            s.load_config()
            say(f"  config: {s.config.get('path')} error={s.config.get('error')} warnings={s.config.get('warnings')}")
            say(f"  active profile: {s.config.get('active_profile')} -> {s.profile}")
            say(f"  chosen: capture={s.capture.name} window={s.window.name}")
            say(f"  live window: {request(server, '/api/live')}")
        except AppError as e:
            say(f"  {e}")
    say(f"host log: {log_file()}")
    say("server log: data/logs/server.log in the repo (or `docker compose logs`; native: `tools/native.sh status`)")
    print("\n".join(out))
    log.info("doctor:\n%s", "\n".join(out))
    return 0


SETUP_STEPS = """Setting up the overlay's browser profile ("Migaku Games"):
  1. Log into Migaku in the study.migaku.com tab.
  2. In the localhost:8765 tab, press Alt+X: Migaku's toolbar appears and stays pinned.
     (Dismiss any Migaku pop-up first; the page says when the toolbar is pinned.)
  3. Close the window. The next hotkey press opens the overlay in the same profile."""


def setup_browser(s: Session) -> int:
    ensure_server(s.server, notify=lambda t, b: notify(s.desktop, t, b))
    s.load_config()
    browser.open_setup_browser(s.desktop, s.server, s.host.get("browser", ""), s.host.get("overlay_profile", True))
    print(SETUP_STEPS)
    s.notify("Set up the overlay browser", "Log into Migaku, then press Alt+X on the migaku-games tab.")
    return 0


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument("--overlay", action="store_true", help="capture into the live window, or hide it if shown")
    p.add_argument("--hide", action="store_true", help="hide the live window and resume a frozen game")
    p.add_argument("--image", type=Path, help="use this image instead of taking a screenshot")
    p.add_argument("--full", action="store_true", help="capture the whole screen instead of a region")
    p.add_argument("--app", action="store_true", help="open in a chromeless app window")
    p.add_argument("--ocr", choices=ENGINES, help="OCR engine (default: the server's)")
    p.add_argument("--game", help="tag the frame with this game name")
    p.add_argument("--server", default=DEFAULT_SERVER, help=f"frame server (default {DEFAULT_SERVER})")
    p.add_argument("--no-open", action="store_true", help="don't open the viewer")
    p.add_argument("--resume", action="store_true", help="unfreeze a game left frozen by --overlay")
    p.add_argument("--doctor", action="store_true", help="check this machine's setup and show where logs are")
    p.add_argument("--setup-browser", action="store_true",
                   help="open the overlay's browser profile normally, to log into Migaku and pin its toolbar")
    p.add_argument("-v", "--verbose", action="store_true", help="show the detailed log on the console too")
    args = p.parse_args()

    setup_logging(args.verbose)
    d = desktop.detect()
    log.info("=== migaku-games %s: %s | python %s | %s | %s", version(), " ".join(sys.argv[1:]) or "(region capture)",
             platform.python_version(), d.os_release, d.describe())
    log.debug("environment: %s", desktop.environment())
    server = args.server.rstrip("/")
    started = time.monotonic()
    s = Session(server)
    try:
        if args.doctor:
            return doctor(server)
        if args.resume:
            freeze.resume()
            return 0
        if args.setup_browser:
            return setup_browser(s)
        with single_instance() as got_lock:
            if not got_lock:
                log.warning("another run is still in progress (double press?); ignoring this one")
                return 0
            ensure_server(server, notify=lambda t, b: notify(d, t, b))
            s.load_config()
            if args.hide:
                hide(s)
            elif args.overlay:
                overlay(s, args.ocr, args.game)
            else:
                capture_once(s, args)
        log.info("=== done in %d ms", (time.monotonic() - started) * 1000)
        return 0
    except AppError as e:
        log.error("=== failed after %d ms: %s", (time.monotonic() - started) * 1000, e)
        notify(d, "migaku-games", str(e), error=True)
        return 1
    except Exception as e:
        log.error("=== crashed after %d ms:\n%s", (time.monotonic() - started) * 1000, traceback.format_exc())
        notify(d, "migaku-games crashed", f"{type(e).__name__}: {e}", error=True)
        return 2


if __name__ == "__main__":
    sys.exit(main())
