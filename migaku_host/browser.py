"""Opening a page in Brave (or another Chromium browser named in the config), per platform."""
import os
import shlex
import subprocess
from pathlib import Path

from .desktop import Desktop, which
from .util import AppError, log, run, state_dir

MAC_APP = "Brave Browser"


def browser_command(d: Desktop, configured: str = "") -> list:
    """The command that starts the browser; `configured` (config host.browser) wins."""
    if configured:
        return shlex.split(configured, posix=d.os != "windows")
    if d.os == "mac":
        return ["open", "-na", MAC_APP, "--args"]
    if d.os == "windows":
        for base in (os.environ.get("ProgramFiles"), os.environ.get("ProgramFiles(x86)"), os.environ.get("LOCALAPPDATA")):
            exe = Path(base or "") / "BraveSoftware" / "Brave-Browser" / "Application" / "brave.exe"
            if base and exe.exists():
                return [str(exe)]
        raise AppError("Brave not found in Program Files or LocalAppData; set host.browser in the config")
    for exe in ("brave-browser", "brave", "brave-browser-stable"):
        if which(exe):
            return [exe]
    if which("flatpak") and run(["flatpak", "info", "com.brave.Browser"], check=False).returncode == 0:
        return ["flatpak", "run", "com.brave.Browser"]
    raise AppError("Brave not found (brave-browser, brave, or the com.brave.Browser Flatpak); set host.browser in the config")


# The named Brave profile inside the overlay's own browser directory (Chromium --profile-directory).
OVERLAY_PROFILE_NAME = "Migaku Games"


def overlay_profile(configured=True) -> Path:
    """The live window's browser profile directory (Chromium's --user-data-dir): the configured
    path (host.overlay_profile), or by default its own under the state directory. Log into
    Migaku there once."""
    if isinstance(configured, str) and configured.strip():
        return Path(os.path.expanduser(configured.strip()))
    return state_dir() / "overlay-browser"


def is_overlay_process(pid) -> bool:
    """Whether a browser process runs the overlay profile (Linux; window backends use it to tell
    the overlay's window from other browser windows)."""
    try:
        cmdline = Path(f"/proc/{pid}/cmdline").read_bytes().decode(errors="replace")
    except (OSError, ValueError):
        return False
    # Chromium rewrites its process title, joining the arguments into one string.
    return f"--profile-directory={OVERLAY_PROFILE_NAME}" in cmdline.replace("\0", " ")


def open_url(d: Desktop, url: str, app: bool, configured: str = "", overlay: bool = False, profile=True) -> None:
    """`overlay`: open the live window in the overlay's own browser instance and named profile,
    started fullscreen (Chromium applies that only when it starts the instance; the page also
    goes fullscreen itself on the first click or key). A separate instance keeps it apart from
    your everyday browsing and its Migaku login; --setup-browser opens it as a normal window."""
    if d.os == "mac" and not configured and not app:
        run(["open", "-a", MAC_APP, url])
        return
    if overlay:
        cmd = browser_command(d, configured) + [f"--user-data-dir={overlay_profile(profile)}",
                                                f"--profile-directory={OVERLAY_PROFILE_NAME}",
                                                "--new-window", "--start-fullscreen", url]
    else:
        cmd = browser_command(d, configured) + (["--start-maximized", f"--app={url}"] if app else [url])
    log.info("browser: %s", cmd)
    try:
        # Detached, so the browser outlives this command.
        kwargs = {"creationflags": 0x00000008} if d.os == "windows" else {"start_new_session": True}
        subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, **kwargs)
    except OSError as e:
        raise AppError(f"couldn't start the browser ({cmd[0]}): {e}") from None


def open_setup_browser(d: Desktop, server: str, configured: str = "", profile=True) -> None:
    """The overlay profile's browser window with Migaku's site to log in, and the newest frame
    to pin Migaku's toolbar."""
    cmd = browser_command(d, configured) + [f"--user-data-dir={overlay_profile(profile)}",
                                            f"--profile-directory={OVERLAY_PROFILE_NAME}", "--new-window",
                                            "https://study.migaku.com", f"{server}/viewer.html?setup"]
    log.info("browser: %s", cmd)
    subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
