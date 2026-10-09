"""Showing and hiding the long-lived "Migaku Live" browser window, per platform.

Where a desktop gives no way to raise another app's window (GNOME on Wayland, generic Wayland),
the "follow" backend does nothing: the live window just shows each new capture, and you switch
to it yourself (or keep it on a second screen)."""
import ast
import json
import re
import tempfile
import time
from pathlib import Path

from .desktop import Desktop, which
from .util import AppError, log, run

LIVE_TITLE = "Migaku Live"  # names the window; must match the title set in web/viewer.html


def script_vars(**values) -> str:
    return "".join(f"const {k} = {json.dumps(v)};\n" for k, v in values.items())


class Backend:
    name = ""
    needs = ()
    desktops = ()
    can_raise = True

    def suits(self, d: Desktop) -> bool:
        return any(all(p in ("*", v) for p, v in zip(pat, (d.os, d.session, d.desktop))) for pat in self.desktops)

    def missing(self) -> list:
        return [n for n in self.needs if not which(n)]

    def note_focus(self) -> None:
        """Called before the capture, while the game still has focus (the screenshot portal takes
        it briefly); backends that return to the game afterwards remember it here."""

    def show(self) -> None:
        raise NotImplementedError

    def hide(self) -> None:
        raise NotImplementedError


class MacJXA(Backend):
    name, needs, desktops = "jxa", ("osascript",), [("mac", "*", "*")]
    SCRIPT = """
ObjC.import("AppKit");
const brave = Application(browser);
const w = brave.windows().find((w) => w.name().includes(title));
if (!w) { "no window titled " + title; }
else if (show) {
  const f = $.NSScreen.mainScreen.frame;
  w.minimized = false;
  w.bounds = { x: 0, y: 0, width: f.size.width, height: f.size.height };
  w.index = 1;
  brave.activate();
  "shown";
} else { w.minimized = true; "hidden"; }
"""

    def __init__(self, browser="Brave Browser"):
        self.browser = browser

    def _run(self, show):
        r = run(["osascript", "-l", "JavaScript", "-e", script_vars(browser=self.browser, title=LIVE_TITLE, show=show) + self.SCRIPT],
                check=False)
        if r.returncode:
            raise AppError(f"couldn't {'show' if show else 'hide'} the live window (macOS may need permission for "
                           f"the terminal to control {self.browser}): {r.stderr.strip()[:200]}")
        log.info("window: %s", r.stdout.strip())

    def show(self):
        self._run(True)

    def hide(self):
        self._run(False)


class KWin(Backend):
    """KDE Plasma 5/6: a one-off KWin script over D-Bus (dbus-send, gdbus or qdbus6)."""
    name, desktops = "kwin", [("linux", "*", "kde")]
    # Window classes of Chromium browsers (Brave by default; host.browser may name another), so a
    # terminal or editor whose title mentions "Migaku Live" is never moved.
    BROWSERS = ["brave", "chrom", "vivaldi", "edge", "opera"]
    SCRIPT = """
const p6 = !!workspace.windowList;
const wins = p6 ? workspace.windowList() : workspace.clientList();
let found = 0;
for (const w of wins) {
  if (!w.caption.includes(title)) continue;
  const cls = (String(w.resourceClass) + " " + String(w.resourceName)).toLowerCase();
  if (!browsers.some((b) => cls.includes(b))) continue;
  found++;
  if (show) {
    // Onto the screen that was just captured (the active one), then fullscreen and focus.
    if (p6) workspace.sendToOutput(w, workspace.activeOutput); else workspace.sendClientToScreen(w, workspace.activeScreen);
    w.minimized = false;
    w.fullScreen = true;
    if (p6) workspace.activeWindow = w; else workspace.activeClient = w;
  } else {
    w.minimized = true;
  }
}
print("migaku-games: " + (show ? "show" : "hide") + " matched " + found + " window(s)");
"""

    def missing(self):
        return [] if self.dbus_tool() else ["dbus-send (or gdbus / qdbus6)"]

    @staticmethod
    def dbus_tool():
        return next((t for t in ("dbus-send", "gdbus", "qdbus6", "qdbus") if which(t)), None)

    def call(self, method, *args):
        tool = self.dbus_tool()
        if tool == "dbus-send":
            cmd = ["dbus-send", "--session", "--print-reply", "--dest=org.kde.KWin", "/Scripting",
                   f"org.kde.kwin.Scripting.{method}", *[f"string:{a}" for a in args]]
        elif tool == "gdbus":
            cmd = ["gdbus", "call", "--session", "--dest", "org.kde.KWin", "--object-path", "/Scripting",
                   "--method", f"org.kde.kwin.Scripting.{method}", *args]
        else:
            cmd = [tool, "org.kde.KWin", "/Scripting", f"org.kde.kwin.Scripting.{method}", *args]
        return run(cmd, check=False)

    def call_path(self, path, method):
        """A no-argument method on another KWin object (a loaded script's org.kde.kwin.Script)."""
        tool = self.dbus_tool()
        if tool == "dbus-send":
            cmd = ["dbus-send", "--session", "--print-reply", "--dest=org.kde.KWin", path, f"org.kde.kwin.Script.{method}"]
        elif tool == "gdbus":
            cmd = ["gdbus", "call", "--session", "--dest", "org.kde.KWin", "--object-path", path,
                   "--method", f"org.kde.kwin.Script.{method}"]
        else:
            cmd = [tool, "org.kde.KWin", path, f"org.kde.kwin.Script.{method}"]
        return run(cmd, check=False)

    @staticmethod
    def script_id(output: str):
        """loadScript's reply: 'int32 3' (dbus-send), '(3,)' (gdbus) or '3' (qdbus); -1 is a failure."""
        found = re.findall(r"-?\d+", output.strip().splitlines()[-1] if output.strip() else "")
        return int(found[-1]) if found and int(found[-1]) >= 0 else None

    def _run(self, show):
        with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False) as f:
            f.write(script_vars(title=LIVE_TITLE, show=show, browsers=self.BROWSERS) + self.SCRIPT)
        try:
            self.call("unloadScript", "migaku-live")  # in case an earlier run left it loaded
            loaded = self.call("loadScript", f.name, "migaku-live")
            if loaded.returncode:
                raise AppError(f"KWin scripting failed: {loaded.stderr.strip()[:200]}")
            # KWin reads the script file in the background, so it mustn't be unloaded (or the file
            # deleted) until it has run. The script's own run() replies only once it has (Plasma 6:
            # /Scripting/Script<id>, Plasma 5: /<id>); Scripting.start() returns at once.
            sid = self.script_id(loaded.stdout)
            ran = sid is not None and any(self.call_path(path, "run").returncode == 0
                                          for path in (f"/Scripting/Script{sid}", f"/{sid}"))
            if not ran:
                log.info("window: couldn't run KWin script %s directly; starting all loaded scripts", sid)
                started = self.call("start")
                if started.returncode:
                    raise AppError(f"KWin script didn't start: {started.stderr.strip()[:200]}")
                time.sleep(0.3)  # let KWin load and run it before it's unloaded
            self.call("unloadScript", "migaku-live")
            log.info("window: KWin script ran (its match count is in the KWin log: journalctl --user -b | grep migaku-games)")
        finally:
            Path(f.name).unlink(missing_ok=True)

    def show(self):
        self._run(True)

    def hide(self):
        self._run(False)


class Sway(Backend):
    name, needs, desktops = "sway", ("swaymsg",), [("linux", "wayland", "sway")]

    def show(self):
        run(["swaymsg", f'[title="{LIVE_TITLE}"] scratchpad show, fullscreen enable, focus'], check=False)

    def hide(self):
        run(["swaymsg", f'[title="{LIVE_TITLE}"] fullscreen disable, move scratchpad'])


class Hyprland(Backend):
    name, needs, desktops = "hyprland", ("hyprctl",), [("linux", "wayland", "hyprland")]

    def show(self):
        run(["hyprctl", "dispatch", "movetoworkspace", f"e+0,title:{LIVE_TITLE}"])
        run(["hyprctl", "dispatch", "focuswindow", f"title:{LIVE_TITLE}"])
        run(["hyprctl", "dispatch", "fullscreen", "0"], check=False)

    def hide(self):
        run(["hyprctl", "dispatch", "movetoworkspacesilent", f"special:migaku,title:{LIVE_TITLE}"])


class Xdotool(Backend):
    """Any X11 window manager."""
    name, needs, desktops = "xdotool", ("xdotool",), [("linux", "x11", "*")]

    def show(self):
        run(["xdotool", "search", "--name", LIVE_TITLE, "windowactivate", "--sync"])
        if which("wmctrl"):
            run(["wmctrl", "-r", LIVE_TITLE, "-b", "add,fullscreen"], check=False)

    def hide(self):
        run(["xdotool", "search", "--name", LIVE_TITLE, "windowminimize"])


class Windows(Backend):
    """user32 through ctypes: no extra tools."""
    name, desktops = "windows", [("windows", "*", "*")]

    def _find(self):
        import ctypes
        from ctypes import wintypes
        user32 = ctypes.windll.user32
        found = []

        @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
        def each(hwnd, _):
            length = user32.GetWindowTextLengthW(hwnd)
            if length:
                buf = ctypes.create_unicode_buffer(length + 1)
                user32.GetWindowTextW(hwnd, buf, length + 1)
                if LIVE_TITLE in buf.value:
                    found.append((hwnd, buf.value))
            return True

        user32.EnumWindows(each, 0)
        log.info("window: %d match(es): %s", len(found), [t for _, t in found])
        return user32, [h for h, _ in found]

    def show(self):
        user32, hwnds = self._find()
        for h in hwnds:
            user32.ShowWindow(h, 3)  # SW_MAXIMIZE
            if not user32.SetForegroundWindow(h):
                log.warning("window: Windows refused to bring the live window to the front")

    def hide(self):
        user32, hwnds = self._find()
        for h in hwnds:
            user32.ShowWindow(h, 6)  # SW_MINIMIZE


class Gnome(Backend):
    """GNOME through two shell extensions: Window Calls
    (https://extensions.gnome.org/extension/4724/window-calls/) to list windows, and Activate
    Window By Title (https://extensions.gnome.org/extension/5021/activate-window-by-title/) to
    focus one with a proper timestamp, the only way another app can raise a window on GNOME
    Wayland. The overlay is never minimised, maximised or kept above: changing a fullscreen
    window's stacking from an extension once deadlocked GNOME Shell. Showing remembers the
    focused window (the game) and hiding focuses it again. Without the extensions this falls
    back to follow mode."""
    name, needs, desktops = "gnome", ("gdbus",), [("linux", "*", "gnome")]
    CALLS = ("/org/gnome/Shell/Extensions/Windows", "org.gnome.Shell.Extensions.Windows")
    ACTIVATE = ("/de/lucaswerkmeister/ActivateWindowByTitle", "de.lucaswerkmeister.ActivateWindowByTitle")

    def missing(self):
        if not which("gdbus"):
            return ["gdbus"]
        missing = []
        for (path, iface), uuid in ((self.CALLS, "window-calls@domandoman.xyz"),
                                    (self.ACTIVATE, "activate-window-by-title@lucaswerkmeister.de")):
            probe = run(["gdbus", "introspect", "--session", "--dest", "org.gnome.Shell", "--object-path", path],
                        check=False, timeout=5)
            if iface not in probe.stdout:
                missing.append(f"the GNOME extension {uuid}")
        return missing

    @staticmethod
    def call(target, method, *args):
        path, iface = target
        return run(["gdbus", "call", "--session", "--dest", "org.gnome.Shell", "--object-path", path,
                    "--method", f"{iface}.{method}", *[str(a) for a in args]], timeout=5)

    def windows(self) -> list:
        # gdbus prints a GVariant tuple holding one JSON string: ('[{...}]',)
        return json.loads(ast.literal_eval(self.call(self.CALLS, "List").stdout.strip())[0])

    @staticmethod
    def is_live(w) -> bool:
        """A browser window titled "Migaku Live" (a terminal mentioning it is never touched)."""
        return LIVE_TITLE in (w.get("title") or "") and any(b in (w.get("wm_class") or "").lower() for b in KWin.BROWSERS)

    @staticmethod
    def game_file():
        from .util import runtime_dir
        return runtime_dir() / "game-window"

    def activate(self, wid) -> None:
        self.call(self.ACTIVATE, "activateById", wid)

    def note_focus(self):
        focused = next((w for w in self.windows() if w.get("focus") and not self.is_live(w)), None)
        if focused:
            self.game_file().write_text(str(focused["id"]))
            log.info("window: will return to %s (%s)", focused["id"], focused.get("wm_class"))
        else:
            log.info("window: no focused window to return to")

    def show(self):
        live = [w["id"] for w in self.windows() if self.is_live(w)]
        log.info("window: %d live window(s): %s", len(live), live)
        if not live:
            raise AppError("no Migaku Live window to raise")
        self.activate(live[0])

    def hide(self):
        try:
            wid = int(self.game_file().read_text())
        except (OSError, ValueError):
            log.info("window: no game window recorded; leaving focus alone")
            return
        if any(w["id"] == wid for w in self.windows()):
            self.activate(wid)
        else:
            log.info("window: game window %s is gone", wid)
        self.game_file().unlink(missing_ok=True)


class Follow(Backend):
    """No window control: the live window follows new captures; switch to it yourself."""
    name, desktops, can_raise = "follow", [("*", "*", "*")], False

    def show(self):
        log.info("window: follow mode, not raising (the live window shows the new frame)")

    def hide(self):
        log.info("window: follow mode, nothing to hide")


BACKENDS = [MacJXA(), Windows(), KWin(), Gnome(), Sway(), Hyprland(), Xdotool(), Follow()]
NAMES = [b.name for b in BACKENDS]


def choose(d: Desktop, preferred: str = "auto") -> Backend:
    if preferred and preferred != "auto":
        backend = next((b for b in BACKENDS if b.name == preferred), None)
        if backend is None:
            raise AppError(f"unknown window backend {preferred!r} (one of {', '.join(NAMES)})")
        if backend.missing():
            raise AppError(f"window backend {preferred} needs {', '.join(backend.missing())}")
        return backend
    for b in BACKENDS:
        if b.suits(d) and not b.missing():
            return b
    return BACKENDS[-1]  # follow


def describe_unsupported(d: Desktop) -> str:
    if d.os == "linux" and d.session == "wayland" and d.desktop == "gnome":
        return "GNOME on Wayland doesn't let other apps raise windows; install the Window Calls and Activate Window By Title extensions"
    return ""

