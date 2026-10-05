"""Screenshots, per platform. Modes: "screen" (the monitor under the mouse, or the focused one),
"full" (all monitors) and "region" (drag-select). Each backend says which desktops it suits and
which programs it needs; choose() picks the first that fits, or the one named in the config."""
import json
import sys
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


BACKENDS = [MacScreencapture(), WindowsPowerShell(), Spectacle(), GnomeScreenshot(), Grim(), Maim(), Scrot(),
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
    hint = {"kde": "spectacle", "gnome": "gnome-screenshot", "sway": "grim", "hyprland": "grim"}.get(d.desktop, "maim or scrot")
    raise AppError(f"no screenshot tool found for {d.desktop}/{d.session}; install {hint}")


def capture(backend: Backend, dest: Path, mode: str) -> None:
    if mode not in backend.modes:
        raise AppError(f"{backend.name} can't do {mode} captures (only {', '.join(backend.modes)})")
    log.info("capture: %s mode=%s -> %s", backend.name, mode, dest)
    backend.capture(dest, mode)
    if not dest.exists() or not dest.stat().st_size:
        raise AppError("capture cancelled or produced no image")
    log.info("capture: %d bytes", dest.stat().st_size)
