"""The host client's platform layer (stdlib only): python3 -m unittest tests.test_host

Runs anywhere; CI also runs it on several Linux distributions (Fedora, Ubuntu, Debian, Arch,
Alpine) with and without desktop tools installed.
"""
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from migaku_host import capture, desktop, freeze, notify, util, window  # noqa: E402

KDE_WAYLAND = {"XDG_CURRENT_DESKTOP": "KDE", "XDG_SESSION_TYPE": "wayland", "WAYLAND_DISPLAY": "wayland-0"}
GNOME_WAYLAND = {"XDG_CURRENT_DESKTOP": "ubuntu:GNOME", "XDG_SESSION_TYPE": "wayland", "WAYLAND_DISPLAY": "wayland-0"}
SWAY = {"XDG_CURRENT_DESKTOP": "sway", "WAYLAND_DISPLAY": "wayland-1", "SWAYSOCK": "/run/sway.sock"}
X11_XFCE = {"XDG_CURRENT_DESKTOP": "XFCE", "DISPLAY": ":0"}


def linux(env):
    return desktop.detect(env, system="linux")


def having(*tools):
    """Patch `which` in every host module so only these tools exist."""
    fake = lambda name: f"/usr/bin/{name}" if name in tools else None
    return mock.patch.multiple("migaku_host.desktop", which=fake), mock.patch.object(capture, "which", fake), \
        mock.patch.object(window, "which", fake), mock.patch.object(capture, "_gi_available", lambda: "gi" in tools)


class DetectTest(unittest.TestCase):
    def test_desktops(self):
        self.assertEqual((linux(KDE_WAYLAND).session, linux(KDE_WAYLAND).desktop), ("wayland", "kde"))
        self.assertEqual(linux(GNOME_WAYLAND).desktop, "gnome")
        self.assertEqual(linux(SWAY).desktop, "sway")
        self.assertEqual((linux(X11_XFCE).session, linux(X11_XFCE).desktop), ("x11", "other"))
        self.assertEqual(linux({}).session, "none")
        self.assertEqual(desktop.detect({}, system="darwin").os, "mac")
        self.assertEqual(desktop.detect({}, system="win32").os, "windows")

    def test_environment_only_lists_known_keys(self):
        env = desktop.environment({"XDG_SESSION_TYPE": "wayland", "SECRET_TOKEN": "x"})
        self.assertEqual(env, {"XDG_SESSION_TYPE": "wayland"})


class GnomeWindowTest(unittest.TestCase):
    LIST = json.dumps([{"id": 7, "title": "Migaku Live", "wm_class": "brave-browser"},
                       {"id": 8, "title": "Migaku Live notes - Terminal", "wm_class": "org.gnome.Ptyxis"},
                       {"id": 9, "title": "FINAL FANTASY VIII", "wm_class": "steam_app_39150"}])

    def run_gnome(self, fullscreen):
        calls = []

        def fake_run(cmd, **_):
            method = cmd[cmd.index("--method") + 1].rsplit(".", 1)[1]
            calls.append([method] + cmd[cmd.index("--method") + 2:])
            out = {"List": repr((self.LIST,)), "Details": repr((json.dumps({"id": 7, "fullscreen": fullscreen}),))}
            return subprocess.CompletedProcess(cmd, 0, out.get(method, "()"), "")

        with mock.patch.object(window, "run", fake_run):
            window.Gnome().show()
            window.Gnome().hide()
        return calls

    def test_show_raises_only_browser_live_windows(self):
        self.assertEqual(self.run_gnome(False), [["List"], ["Unminimize", "7"], ["Details", "7"], ["Maximize", "7"],
                                                 ["MakeAbove", "7"], ["Activate", "7"],
                                                 ["List"], ["UnmakeAbove", "7"], ["Minimize", "7"]])

    def test_show_keeps_a_fullscreen_overlay_fullscreen(self):
        self.assertNotIn(["Maximize", "7"], self.run_gnome(True))


class ChooseTest(unittest.TestCase):
    def choose(self, env, tools, which_kind, preferred="auto"):
        patches = having(*tools)
        for p in patches:
            p.start()
        try:
            module = capture if which_kind == "capture" else window
            return module.choose(linux(env), preferred).name
        finally:
            for p in patches:
                p.stop()

    def test_capture_per_desktop(self):
        self.assertEqual(self.choose(KDE_WAYLAND, ["spectacle", "grim"], "capture"), "spectacle")
        self.assertEqual(self.choose(GNOME_WAYLAND, ["gnome-screenshot"], "capture"), "gnome-screenshot")
        self.assertEqual(self.choose(GNOME_WAYLAND, ["gi", "gnome-screenshot"], "capture"), "portal")
        self.assertEqual(self.choose(X11_XFCE, ["gi", "scrot"], "capture"), "scrot")
        self.assertEqual(self.choose(SWAY, ["grim"], "capture"), "grim")
        self.assertEqual(self.choose(X11_XFCE, ["scrot"], "capture"), "scrot")

    def test_capture_falls_back_to_any_installed_tool(self):
        self.assertEqual(self.choose(KDE_WAYLAND, ["grim"], "capture"), "grim")

    def test_capture_missing_everything_says_what_to_install(self):
        with self.assertRaisesRegex(util.AppError, "spectacle"):
            self.choose(KDE_WAYLAND, [], "capture")

    def test_capture_configured_backend(self):
        self.assertEqual(self.choose(KDE_WAYLAND, ["spectacle", "maim"], "capture", "maim"), "maim")
        with self.assertRaisesRegex(util.AppError, "needs"):
            self.choose(KDE_WAYLAND, ["spectacle"], "capture", "maim")
        with self.assertRaisesRegex(util.AppError, "unknown"):
            self.choose(KDE_WAYLAND, ["spectacle"], "capture", "nope")

    def test_window_per_desktop_and_follow_fallback(self):
        self.assertEqual(self.choose(KDE_WAYLAND, ["dbus-send"], "window"), "kwin")
        self.assertEqual(self.choose(KDE_WAYLAND, ["gdbus"], "window"), "kwin")
        self.assertEqual(self.choose(KDE_WAYLAND, [], "window"), "follow")
        self.assertEqual(self.choose(GNOME_WAYLAND, ["dbus-send"], "window"), "follow")
        with mock.patch.object(window, "run", lambda *a, **k: subprocess.CompletedProcess(a, 0, window.Gnome.IFACE, "")):
            self.assertEqual(self.choose(GNOME_WAYLAND, ["gdbus"], "window"), "gnome")
        with mock.patch.object(window, "run", lambda *a, **k: subprocess.CompletedProcess(a, 1, "", "no such object")):
            self.assertEqual(self.choose(GNOME_WAYLAND, ["gdbus"], "window"), "follow")
        self.assertEqual(self.choose(SWAY, ["swaymsg"], "window"), "sway")
        self.assertEqual(self.choose(X11_XFCE, ["xdotool"], "window"), "xdotool")

    def test_config_lists_every_backend(self):
        sys.path.insert(0, str(ROOT))
        try:
            import config
        except ImportError:  # no PyYAML on this machine: nothing to compare
            self.skipTest("config.py needs PyYAML")
        self.assertEqual(set(config.CAPTURE_BACKENDS) - {"auto"}, set(capture.NAMES))
        self.assertEqual(set(config.WINDOW_BACKENDS) - {"auto"}, set(window.NAMES))


class KWinTest(unittest.TestCase):
    def test_script_id_from_each_dbus_tool(self):
        sid = window.KWin.script_id
        self.assertEqual(sid("method return time=1.2 sender=:1.5 -> destination=:1.9 serial=812\n   int32 7\n"), 7)
        self.assertEqual(sid("(7,)\n"), 7)
        self.assertEqual(sid("7\n"), 7)
        self.assertIsNone(sid("   int32 -1\n"))  # KWin couldn't load it
        self.assertIsNone(sid(""))


class NativeServiceTest(unittest.TestCase):
    def test_systemd_unit_means_a_native_install(self):
        from migaku_host import server_api
        with tempfile.TemporaryDirectory() as d, mock.patch.dict(os.environ, {"XDG_CONFIG_HOME": d}), \
                mock.patch.object(server_api.sys, "platform", "linux"):
            self.assertIsNone(server_api.native_service())
            unit = Path(d) / "systemd" / "user" / server_api.NATIVE_UNIT
            unit.parent.mkdir(parents=True)
            unit.touch()
            self.assertEqual(server_api.native_service(), ["systemctl", "--user", "start", server_api.NATIVE_UNIT])


class FreezeTest(unittest.TestCase):
    PROCS = {1: (0, ["/sbin/init"]), 100: (1, ["/steam/reaper", "SteamLaunch", "AppId=39150", "--"]),
             101: (100, ["Z:\\games\\FF8_EN.exe"]), 102: (101, ["wineserver"]),
             200: (1, ["/usr/bin/duckstation-qt"]), 300: (1, ["bash", "-c", "duckstation in an argument"])}

    def test_steam_game_by_reaper(self):
        self.assertEqual(freeze.game_process("", self.PROCS), 100)
        self.assertEqual(sorted(freeze.process_tree(100, self.PROCS)), [100, 101, 102])

    def test_flatpak_steam_game_outside_the_reaper_tree(self):
        # Flatpak Steam: reaper 100 (its tree 100-102), the game's container 500 started by
        # flatpak-portal 400 with its child 501, and an unrelated Steam process 600.
        procs = dict(self.PROCS)
        procs.update({400: (1, ["flatpak-portal"]), 500: (400, ["bwrap", "--", "pv-adverb"]),
                      501: (500, ["Z:\\games\\FF8_EN.exe"]), 600: (1, ["steamwebhelper"])})
        env = {500: {"SteamAppId": "39150"}, 501: {"SteamAppId": "39150"}, 600: {"SteamAppId": "730"}}
        tree = freeze.steam_game_tree(100, procs, environ=lambda pid: env.get(pid, {}))
        self.assertEqual(sorted(tree), [100, 101, 102, 500, 501])

    def test_proton_freezes_only_the_game_executables(self):
        procs = {10: (1, ["/steam/ubuntu12_32/reaper", "SteamLaunch", "AppId=1026680"]),
                 11: (10, ["/steam/steamapps/common/SteamLinuxRuntime_sniper/pv-adverb"]),
                 12: (11, ["python3", "/steam/steamapps/common/Proton - Experimental/proton"]),
                 13: (12, ["/steam/steamapps/common/Proton - Experimental/files/bin/wineserver"]),
                 14: (12, ["c:\\windows\\system32\\steam.exe"]),
                 15: (14, ["C:\\windows\\system32\\services.exe"]),
                 16: (14, ["S:\\steamapps\\common\\FINAL FANTASY VIII Remastered\\FFVIII_LAUNCHER.exe"]),
                 17: (16, ["S:\\steamapps\\common\\FINAL FANTASY VIII Remastered\\FFVIII.exe", "jp"])}
        tree = freeze.process_tree(10, procs)
        self.assertEqual(sorted(freeze.game_executables(tree, procs)), [16, 17])
        native = {20: (1, ["/steam/reaper", "SteamLaunch", "AppId=1"]), 21: (20, ["/steam/steamapps/common/Game/game.x86_64"])}
        self.assertEqual(freeze.game_executables(freeze.process_tree(20, native), native), [21])
        unknown = {30: (1, ["/steam/reaper", "SteamLaunch", "AppId=2"]), 31: (30, ["/opt/elsewhere/game"])}
        self.assertEqual(sorted(freeze.game_executables(freeze.process_tree(30, unknown), unknown)), [30, 31])

    def test_pattern_matches_executable_name_only(self):
        self.assertEqual(freeze.game_process("DuckStation", self.PROCS), 200)
        self.assertEqual(freeze.game_process("ff8_en", self.PROCS), 101)  # Wine path with backslashes
        self.assertIsNone(freeze.game_process("retroarch", self.PROCS))

    def test_resume_with_nothing_frozen_is_a_no_op(self):
        with tempfile.TemporaryDirectory() as d, mock.patch.dict(os.environ, {"XDG_RUNTIME_DIR": d}):
            freeze.resume()


class OverlayTest(unittest.TestCase):
    def test_fullscreen_overlay_gets_its_own_profile(self):
        d = linux(GNOME_WAYLAND)
        with mock.patch.object(desktop, "which", lambda n: "/usr/bin/" + n if n == "brave-browser" else None), \
                mock.patch("migaku_host.browser.which", lambda n: "/usr/bin/" + n if n == "brave-browser" else None), \
                mock.patch("subprocess.Popen") as popen:
            from migaku_host import browser
            browser.open_url(d, "http://localhost:8765/viewer.html?live", app=True, fullscreen=True)
            cmd = popen.call_args[0][0]
            self.assertEqual(cmd[0], "brave-browser")
            self.assertTrue(cmd[1].startswith("--user-data-dir=") and cmd[1].endswith("overlay-browser"))
            self.assertEqual(cmd[2:], ["--start-fullscreen", "--app=http://localhost:8765/viewer.html?live"])
            browser.open_url(d, "http://x/", app=True)
            self.assertEqual(popen.call_args[0][0], ["brave-browser", "--start-maximized", "--app=http://x/"])

    def test_clipboard_tool_per_session(self):
        with tempfile.NamedTemporaryFile(suffix=".png") as f:
            for env, tools, expected in [(GNOME_WAYLAND, ["wl-copy", "xclip"], "wl-copy"), (X11_XFCE, ["xclip"], "xclip"),
                                         (GNOME_WAYLAND, ["xclip"], "xclip"), (GNOME_WAYLAND, [], None)]:
                with mock.patch.object(capture, "which", lambda n, t=tools: "/usr/bin/" + n if n in t else None), \
                        mock.patch("subprocess.Popen") as popen:
                    capture.copy_to_clipboard(linux(env), Path(f.name))
                    self.assertEqual(popen.call_args[0][0][0] if popen.called else None, expected)


class UtilTest(unittest.TestCase):
    def test_missing_command_is_an_app_error(self):
        with self.assertRaisesRegex(util.AppError, "not installed"):
            util.run(["definitely-not-a-real-command-xyz"])

    def test_failing_command_reports_stderr(self):
        with self.assertRaisesRegex(util.AppError, "boom"):
            util.run([sys.executable, "-c", "import sys; sys.stderr.write('boom'); sys.exit(3)"])

    def test_lock_rejects_a_second_run(self):
        with tempfile.TemporaryDirectory() as d, mock.patch.dict(os.environ, {"XDG_RUNTIME_DIR": d}):
            child = (f"import sys; sys.path.insert(0, {str(ROOT)!r}); from migaku_host.util import single_instance\n"
                     "with single_instance('t') as got: print(got)")
            with util.single_instance("t") as first:
                second = subprocess.run([sys.executable, "-c", child], capture_output=True, text=True,
                                        env={**os.environ, "XDG_RUNTIME_DIR": d}).stdout.strip()
            self.assertTrue(first)
            self.assertEqual(second, "False")

    def test_notify_without_tools_does_not_raise(self):
        with mock.patch.object(notify, "which", lambda n: None):
            notify.notify(linux(X11_XFCE), "title", "body", error=True)


if __name__ == "__main__":
    unittest.main()
