"""Opening a page in Brave (or another Chromium browser named in the config), per platform."""
import os
import shlex
import subprocess
from pathlib import Path

from .desktop import Desktop, which
from .util import AppError, log, run

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


def open_url(d: Desktop, url: str, app: bool, configured: str = "") -> None:
    if d.os == "mac" and not configured and not app:
        run(["open", "-a", MAC_APP, url])
        return
    cmd = browser_command(d, configured) + ([f"--app={url}"] if app else [url])
    log.info("browser: %s", cmd)
    try:
        # Detached, so the browser outlives this command.
        kwargs = {"creationflags": 0x00000008} if d.os == "windows" else {"start_new_session": True}
        subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, **kwargs)
    except OSError as e:
        raise AppError(f"couldn't start the browser ({cmd[0]}): {e}") from None
