"""Which desktop we're on: OS, session type and desktop environment, from the environment the
hotkey runs in. Every backend choice is made from this, and it's logged on each run."""
import os
import platform
import shutil
import sys
from dataclasses import asdict, dataclass

# Environment variables worth logging when something goes wrong.
ENV_KEYS = ("XDG_CURRENT_DESKTOP", "XDG_SESSION_DESKTOP", "DESKTOP_SESSION", "XDG_SESSION_TYPE",
            "WAYLAND_DISPLAY", "DISPLAY", "KDE_SESSION_VERSION", "SWAYSOCK", "HYPRLAND_INSTANCE_SIGNATURE",
            "XDG_RUNTIME_DIR", "DBUS_SESSION_BUS_ADDRESS", "FLATPAK_ID", "container", "PATH")


@dataclass
class Desktop:
    os: str            # linux | mac | windows | other
    session: str       # wayland | x11 | native (mac/windows) | none
    desktop: str       # kde | gnome | sway | hyprland | other | native
    os_release: str    # e.g. 'Fedora Linux 42 (Bazzite)', 'macOS 26.0', 'Windows 11'

    def describe(self) -> dict:
        return asdict(self)


def _os_release() -> str:
    if sys.platform == "darwin":
        return f"macOS {platform.mac_ver()[0]}"
    if os.name == "nt":
        return f"Windows {platform.release()} ({platform.version()})"
    try:
        info = dict(line.split("=", 1) for line in open("/etc/os-release").read().splitlines() if "=" in line)
        return info.get("PRETTY_NAME", "").strip('"') or "Linux"
    except OSError:
        return f"{platform.system()} {platform.release()}"


def detect(env=None, system=None) -> Desktop:
    """`env`/`system` (sys.platform) default to this machine's; tests pass others."""
    env = os.environ if env is None else env
    system = system or sys.platform
    if system == "darwin":
        return Desktop("mac", "native", "native", _os_release())
    if system in ("win32", "cygwin"):
        return Desktop("windows", "native", "native", _os_release())
    if not system.startswith("linux"):
        return Desktop("other", "none", "other", _os_release())
    session = (env.get("XDG_SESSION_TYPE") or "").lower()
    if session not in ("wayland", "x11"):
        session = "wayland" if env.get("WAYLAND_DISPLAY") else "x11" if env.get("DISPLAY") else "none"
    names = " ".join(env.get(k, "") for k in ("XDG_CURRENT_DESKTOP", "XDG_SESSION_DESKTOP", "DESKTOP_SESSION")).lower()
    if "kde" in names or "plasma" in names or env.get("KDE_SESSION_VERSION"):
        desktop = "kde"
    elif "gnome" in names or "ubuntu" in names or "unity" in names:
        desktop = "gnome"
    elif "sway" in names or env.get("SWAYSOCK"):
        desktop = "sway"
    elif "hyprland" in names or env.get("HYPRLAND_INSTANCE_SIGNATURE"):
        desktop = "hyprland"
    else:
        desktop = "other"
    return Desktop("linux", session, desktop, _os_release())


def which(name: str):
    return shutil.which(name)


def environment(env=None) -> dict:
    env = os.environ if env is None else env
    return {k: env[k] for k in ENV_KEYS if k in env}
