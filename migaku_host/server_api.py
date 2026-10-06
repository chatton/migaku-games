"""Talking to the frame server (and starting it: its container, or a native install's service), with every call logged."""
import json
import os
import shutil
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from .util import AppError, log, run

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_SERVER = "http://localhost:8765"
# A native install's service names (tools/native.sh).
NATIVE_UNIT = "migaku-games.service"
NATIVE_LABEL = "com.migaku-games.server"


def request(server: str, path: str, data=None, method=None, timeout: float = 5):
    """JSON in/out. Raises AppError with the server's error message, or that it's unreachable."""
    url = server + path
    body = data if isinstance(data, bytes) or data is None else json.dumps(data).encode()
    req = urllib.request.Request(url, data=body, method=method or ("POST" if body is not None else "GET"))
    start = time.monotonic()
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read()
            log.debug("http %s %s -> %d (%d ms, %d bytes)", req.method, path, resp.status, (time.monotonic() - start) * 1000, len(raw))
            return json.loads(raw) if raw[:1] in (b"{", b"[") else None
    except urllib.error.HTTPError as e:
        detail = e.read()[:500].decode(errors="replace")
        log.warning("http %s %s -> %d (%d ms): %s", req.method, path, e.code, (time.monotonic() - start) * 1000, detail)
        try:
            detail = json.loads(detail).get("error", detail)
        except ValueError:
            pass
        raise AppError(f"server error on {path}: {detail}") from None
    except (urllib.error.URLError, OSError) as e:
        log.warning("http %s %s failed (%d ms): %s", req.method, path, (time.monotonic() - start) * 1000, e)
        raise AppError(f"frame server not reachable at {server} ({getattr(e, 'reason', e)})") from None


def server_up(server: str) -> bool:
    try:
        request(server, "/api/config", timeout=2)
        return True
    except AppError:
        return False


def wait_until_up(server: str, timeout: float) -> bool:
    end = time.time() + timeout
    while time.time() < end:
        if server_up(server):
            return True
        time.sleep(0.5)
    return False


def native_service():
    """The command that starts a native install's service (tools/native.sh), if there is one."""
    if sys.platform.startswith("linux"):
        unit = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config") / "systemd" / "user" / NATIVE_UNIT
        if unit.exists():
            return ["systemctl", "--user", "start", NATIVE_UNIT]
    elif sys.platform == "darwin":
        plist = Path.home() / "Library" / "LaunchAgents" / f"{NATIVE_LABEL}.plist"
        if plist.exists():  # kickstart needs the agent loaded; bootstrap loads (and starts) it
            domain = f"gui/{os.getuid()}"
            return ["sh", "-c", f"launchctl kickstart {domain}/{NATIVE_LABEL} || launchctl bootstrap {domain} '{plist}'"]
    return None


def compose_command(prefer: str = "") -> list:
    """docker compose or podman compose (podman first on Linux, where it's the usual default)."""
    order = [["docker", "compose"], ["podman", "compose"]]
    if prefer == "podman" or (not prefer and shutil.which("podman") and not shutil.which("docker")):
        order.reverse()
    for cmd in order:
        if shutil.which(cmd[0]):
            log.info("compose: using %s", " ".join(cmd))
            return cmd
    raise AppError("neither docker nor podman found; start the frame server yourself")


def ensure_server(server: str, notify=None) -> None:
    """Make sure the server answers; the default (local) one is started with compose if needed."""
    if server_up(server):
        return
    if server != DEFAULT_SERVER:
        log.info("waiting for the frame server at %s", server)
        if not wait_until_up(server, 120):
            raise AppError(f"no frame server at {server}")
        return
    if notify:
        notify("Starting the frame server…", "First start can take a minute.")
    native = native_service()
    if native:  # installed with tools/native.sh: the service, not a container
        log.info("frame server: native install, starting its service")
        run(native, cwd=ROOT)
        if not wait_until_up(server, 60):
            raise AppError("the native frame server didn't come up; see data/logs/server.log and `tools/native.sh status`")
        return
    run(compose_command() + ["up", "-d"], cwd=ROOT, timeout=900)
    if not wait_until_up(server, 90):
        raise AppError("the frame server container didn't come up; see `docker compose logs`")


def upload(server: str, image: Path, engine=None, game=None, wait=True) -> dict:
    params = {"ocr": engine, "game": game, "wait": None if wait else "0"}
    query = urllib.parse.urlencode({k: v for k, v in params.items() if v})
    data = image.read_bytes()
    log.info("upload: %s (%d bytes) game=%s wait=%s", image.name, len(data), game, wait)
    return request(server, f"/api/frames?{query}", data=data, timeout=120)


def wait_live(server: str, until, timeout: float) -> bool:
    end = time.time() + timeout
    while time.time() < end:
        state = request(server, "/api/live", timeout=2)
        if until(state):
            return True
        time.sleep(0.1)
    log.info("live: gave up waiting after %.1fs; last state %s", timeout, state)
    return False
