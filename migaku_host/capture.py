"""Screenshots, per platform. Modes: "screen" (the monitor under the mouse, or the focused one),
"full" (all monitors) and "region" (drag-select). Each backend says which desktops it suits and
which programs it needs; choose() picks the first that fits, or the one named in the config."""
import json
import os
import shutil
import subprocess
import sys
import time
import urllib.parse
from pathlib import Path

from .desktop import Desktop, which
from .util import AppError, log, run

# Windows: PowerShell + .NET, nothing to install. Captures the screen under the cursor or all
# screens. (Per-monitor DPI scaling can crop the capture on scaled displays; logged if so.)
_PS_CAPTURE = r"""
Add-Type -AssemblyName System.Windows.Forms, System.Drawing
Add-Type 'using System.Runtime.InteropServices; public class D { [DllImport("user32.dll")] public static extern bool SetProcessDPIAware(); }'
[D]::SetProcessDPIAware() | Out-Null
$b = if ('{mode}' -eq 'full') { [System.Windows.Forms.SystemInformation]::VirtualScreen } else { [System.Windows.Forms.Screen]::FromPoint([System.Windows.Forms.Cursor]::Position).Bounds }
$bmp = New-Object System.Drawing.Bitmap $b.Width, $b.Height
[System.Drawing.Graphics]::FromImage($bmp).CopyFromScreen($b.Location, [System.Drawing.Point]::Empty, $b.Size)
$bmp.Save('{path}', [System.Drawing.Imaging.ImageFormat]::Png)
Write-Output "$($b.Width)x$($b.Height) at $($b.X),$($b.Y)"
"""


class Backend:
    name = ""
    needs = ()           # programs that must be on PATH
    desktops = ()        # (os, session, desktop) patterns it suits; "*" matches anything
    modes = ("screen", "full", "region")

    def suits(self, d: Desktop) -> bool:
        return any(all(p in ("*", v) for p, v in zip(pat, (d.os, d.session, d.desktop))) for pat in self.desktops)

    def missing(self) -> list:
        return [n for n in self.needs if not which(n)]

    def capture(self, dest: Path, mode: str) -> None:
        raise NotImplementedError


class MacScreencapture(Backend):
    name, needs, desktops = "screencapture", ("screencapture",), [("mac", "*", "*")]

    def capture(self, dest, mode):
        # -m: the main display (screencapture can't target the one under the mouse).
        run(["screencapture", "-x", *{"screen": ["-m"], "full": [], "region": ["-i"]}[mode], dest])


class WindowsPowerShell(Backend):
    name, needs, desktops, modes = "windows", ("powershell",), [("windows", "*", "*")], ("screen", "full")

    def capture(self, dest, mode):
        script = _PS_CAPTURE.replace("{mode}", mode).replace("{path}", str(dest).replace("'", "''"))
        r = run(["powershell", "-NoProfile", "-NonInteractive", "-Command", script])
        log.info("captured %s", r.stdout.strip())


class Spectacle(Backend):
    name, needs, desktops = "spectacle", ("spectacle",), [("linux", "*", "kde")]

    def capture(self, dest, mode):
        # -b background, -n no notification, -o output file; -m monitor under the mouse.
        run(["spectacle", "-b", "-n", {"screen": "-m", "full": "-f", "region": "-r"}[mode], "-o", dest], timeout=120)


def _gi_available() -> bool:
    try:
        import gi  # noqa: F401  (PyGObject: python3-gobject on Fedora, python3-gi on Debian/Ubuntu)
    except ImportError:
        return False
    return True


def _portal_screenshot(interactive: bool, timeout: int) -> str:
    """Ask xdg-desktop-portal for a screenshot and wait for its answer; returns the file URI. The
    answer is a signal sent only to the requesting connection, so this needs PyGObject rather than
    the gdbus CLI."""
    import gi
    gi.require_version("Gio", "2.0")
    from gi.repository import Gio, GLib

    result = {}
    try:
        bus = Gio.bus_get_sync(Gio.BusType.SESSION, None)
        token = f"migaku{os.getpid()}"
        sender = bus.get_unique_name()[1:].replace(".", "_")
        handle = f"/org/freedesktop/portal/desktop/request/{sender}/{token}"
        loop = GLib.MainLoop()

        def on_response(_conn, _sender, _path, _iface, _signal, params):
            result["code"], result["results"] = params.unpack()
            loop.quit()

        sub = bus.signal_subscribe("org.freedesktop.portal.Desktop", "org.freedesktop.portal.Request", "Response",
                                   handle, None, Gio.DBusSignalFlags.NONE, on_response)
        try:
            options = {"handle_token": GLib.Variant("s", token), "interactive": GLib.Variant("b", interactive)}
            bus.call_sync("org.freedesktop.portal.Desktop", "/org/freedesktop/portal/desktop",
                          "org.freedesktop.portal.Screenshot", "Screenshot", GLib.Variant("(sa{sv})", ("", options)),
                          GLib.VariantType("(o)"), Gio.DBusCallFlags.NONE, -1, None)
            GLib.timeout_add_seconds(timeout, loop.quit)
            loop.run()
        finally:
            bus.signal_unsubscribe(sub)
    except GLib.Error as e:
        raise AppError(f"screenshot portal failed: {e.message}") from None
    if "code" not in result:
        raise AppError(f"screenshot portal gave no answer within {timeout}s")
    if result["code"] == 1:
        raise AppError("capture cancelled")
    if result["code"] != 0 or not result["results"].get("uri"):
        raise AppError('screenshot portal refused the capture; allow captures without a prompt with: '
                       'flatpak permission-set screenshot screenshot "" yes')
    return result["results"]["uri"]


class Portal(Backend):
    """xdg-desktop-portal's Screenshot. Recent GNOME on Wayland only lets its own tools capture the
    screen directly, so gnome-screenshot hangs there; the portal works. A non-interactive capture
    needs the saved permission (`flatpak permission-set screenshot screenshot "" yes`), or GNOME
    refuses it; "region" opens GNOME's screenshot UI to pick the area."""
    name, desktops = "portal", [("linux", "wayland", "gnome")]

    def missing(self):
        return [] if _gi_available() else ["python3-gobject"]

    def capture(self, dest, mode):
        start = time.monotonic()
        uri = _portal_screenshot(interactive=(mode == "region"), timeout=120 if mode == "region" else 30)
        src = Path(urllib.parse.unquote(urllib.parse.urlparse(uri).path))
        log.info("portal: %s (%d ms)", src, (time.monotonic() - start) * 1000)
        # The portal saves into ~/Pictures; move it so captures don't pile up there.
        shutil.move(str(src), str(dest))


class GnomeScreenshot(Backend):
    name, needs, desktops = "gnome-screenshot", ("gnome-screenshot",), [("linux", "*", "gnome")]

    def capture(self, dest, mode):
        # No per-monitor option: "screen" takes everything.
        run(["gnome-screenshot", *(["-a"] if mode == "region" else []), "-f", dest], timeout=120)


class Grim(Backend):
    """wlroots compositors (Sway, Hyprland, ...)."""
    name, needs, desktops = "grim", ("grim",), [("linux", "wayland", "sway"), ("linux", "wayland", "hyprland"),
                                                 ("linux", "wayland", "other")]

    def capture(self, dest, mode):
        if mode == "region":
            if not which("slurp"):
                raise AppError("region capture with grim needs slurp")
            geometry = run(["slurp"], timeout=120).stdout.strip()
            run(["grim", "-g", geometry, dest])
            return
        output = self.focused_output() if mode == "screen" else None
        run(["grim", *(["-o", output] if output else []), dest])

    @staticmethod
    def focused_output():
        try:
            if which("swaymsg"):
                outs = json.loads(run(["swaymsg", "-t", "get_outputs", "-r"]).stdout)
                return next((o["name"] for o in outs if o.get("focused")), None)
            if which("hyprctl"):
                mons = json.loads(run(["hyprctl", "monitors", "-j"]).stdout)
                return next((m["name"] for m in mons if m.get("focused")), None)
        except (AppError, ValueError) as e:
            log.warning("couldn't find the focused output, capturing all: %s", e)
        return None


class Maim(Backend):
    name, needs, desktops = "maim", ("maim",), [("linux", "x11", "*")]

    def capture(self, dest, mode):
        run(["maim", *(["-s"] if mode == "region" else []), dest], timeout=120)


class Scrot(Backend):
    name, needs, desktops = "scrot", ("scrot",), [("linux", "x11", "*")]

    def capture(self, dest, mode):
        run(["scrot", "-o", *(["-s"] if mode == "region" else []), dest], timeout=120)


class ImageMagickImport(Backend):
    name, needs, desktops = "import", ("import",), [("linux", "x11", "*")]

    def capture(self, dest, mode):
        run(["import", *([] if mode == "region" else ["-window", "root"]), dest], timeout=120)


BACKENDS = [MacScreencapture(), WindowsPowerShell(), Spectacle(), Portal(), GnomeScreenshot(), Grim(), Maim(), Scrot(),
            ImageMagickImport()]
NAMES = [b.name for b in BACKENDS]


def choose(d: Desktop, preferred: str = "auto") -> Backend:
    """The configured backend, else the first that suits this desktop and is installed, else the
    first installed one at all (e.g. spectacle on a desktop we didn't recognise)."""
    if preferred and preferred != "auto":
        backend = next((b for b in BACKENDS if b.name == preferred), None)
        if backend is None:
            raise AppError(f"unknown capture backend {preferred!r} (one of {', '.join(NAMES)})")
        if backend.missing():
            raise AppError(f"capture backend {preferred} needs {', '.join(backend.missing())}")
        return backend
    fitting = [b for b in BACKENDS if b.suits(d)]
    for b in fitting + [b for b in BACKENDS if b not in fitting]:
        if not b.missing():
            return b
    hint = {"kde": "spectacle", "gnome": "python3-gobject (portal) or gnome-screenshot", "sway": "grim", "hyprland": "grim"}.get(d.desktop, "maim or scrot")
    raise AppError(f"no screenshot tool found for {d.desktop}/{d.session}; install {hint}")


def copy_to_clipboard(d: Desktop, image: Path) -> None:
    """Put the capture on the clipboard, so Ctrl+V in Migaku's card creator adds it however the
    card was started. wl-copy (Wayland) or xclip (X11); skipped with a log line otherwise."""
    if d.os != "linux":
        return
    if d.session == "wayland" and which("wl-copy"):
        cmd = ["wl-copy", "--type", "image/png"]
    elif which("xclip"):
        cmd = ["xclip", "-selection", "clipboard", "-t", "image/png", "-i"]
    else:
        log.info("clipboard: no wl-copy or xclip; not copying the capture")
        return
    with open(image, "rb") as f:
        # wl-copy and xclip stay in the background to serve the clipboard; don't wait on them.
        subprocess.Popen(cmd, stdin=f, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
    log.info("clipboard: capture copied (%s)", cmd[0])


def capture(backend: Backend, dest: Path, mode: str) -> None:
    if mode not in backend.modes:
        raise AppError(f"{backend.name} can't do {mode} captures (only {', '.join(backend.modes)})")
    log.info("capture: %s mode=%s -> %s", backend.name, mode, dest)
    backend.capture(dest, mode)
    if not dest.exists() or not dest.stat().st_size:
        raise AppError("capture cancelled or produced no image")
    log.info("capture: %d bytes", dest.stat().st_size)
