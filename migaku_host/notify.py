"""Desktop notifications, so a failed hotkey press says why instead of doing nothing."""
import subprocess

from .desktop import Desktop, which
from .util import log, log_file

_PS_TOAST = r"""
Add-Type -AssemblyName System.Windows.Forms
$n = New-Object System.Windows.Forms.NotifyIcon
$n.Icon = [System.Drawing.SystemIcons]::{icon}
$n.Visible = $true
$n.ShowBalloonTip(6000, '{title}', '{body}', '{kind}')
Start-Sleep -Seconds 7
$n.Dispose()
"""


def notify(d: Desktop, title: str, body: str = "", error: bool = False) -> None:
    """Best effort: a notification that can't be shown is logged, never raised."""
    level = log.error if error else log.info
    level("notify: %s: %s", title, body)
    if error:
        body = f"{body}\nLog: {log_file()}".strip()
    try:
        if d.os == "mac":
            esc = lambda s: s.replace("\\", "\\\\").replace('"', '\\"')
            subprocess.Popen(["osascript", "-e", f'display notification "{esc(body)}" with title "{esc(title)}"'],
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        elif d.os == "windows":
            esc = lambda s: s.replace("'", "''")
            script = (_PS_TOAST.replace("{title}", esc(title)).replace("{body}", esc(body))
                      .replace("{icon}", "Error" if error else "Information").replace("{kind}", "Error" if error else "Info"))
            subprocess.Popen(["powershell", "-NoProfile", "-WindowStyle", "Hidden", "-Command", script],
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, creationflags=0x08000000)
        elif which("notify-send"):
            subprocess.Popen(["notify-send", "-a", "migaku-games", "-u", "critical" if error else "normal",
                              "-t", "8000", title, body], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        elif which("kdialog"):
            subprocess.Popen(["kdialog", "--title", title, "--passivepopup", body, "8"],
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        else:
            log.warning("notify: no notification tool (notify-send/kdialog); message only in the log")
    except OSError as e:
        log.warning("notify: couldn't show a notification: %s", e)
