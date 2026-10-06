"""Frame server: web UI, OCR, frame storage and retention.

    python3 server.py                       # http://localhost:8765
    python3 server.py --ocr meiki --data ./data --config ./config/config.yaml

The options can also come from the environment (what the container uses):
MIGAKU_OCR, MIGAKU_HOST, MIGAKU_PORT, MIGAKU_DATA, MIGAKU_CONFIG, MIGAKU_ALLOWED_HOSTS.

How the app behaves (retention, game profiles, keybindings, OCR engine) is declared in the
config file (see config.py and config/config.yaml); the web UI shows it and reloads it on
demand. Pinned frames are never pruned; 0 retention hours keeps everything.

API
  GET    /api/config                     the loaded config, plus engines, warnings, error, path
  POST   /api/config/reload              re-read the config file; 400 (old config kept) if invalid
  GET    /api/frames                     newest first
  POST   /api/frames[?ocr=ENGINE&game=NAME&wait=0]
                                         body: image bytes -> {"id", "url", "lines"}; with wait=0
                                         it returns once the picture is saved and OCR runs on
  GET    /api/frames/<id>/ocr            the frame's OCR result, waiting for it if still running
  GET    /api/latest?after=ID            newest frame id; waits (up to 25s) for one newer than ID
  GET    /api/live                       the live viewer: {"open", "focused", "frame"}
  POST   /api/live                       the live viewer reports {"focused", "frame"}
  PUT    /api/frames/<id>/pin            {"pinned": true|false}
  DELETE /api/frames/<id>
  POST   /api/align                      {"ja", "en"} -> {"pairs": [{"ja": [s, e], "en": [s, e]}]} matched words
  POST   /api/log                        a line from a page for the server log (e.g. clipboard copies)
  POST   /debug                          viewer posts its DOM here (dev aid)
"""
import argparse
import io
from datetime import datetime
import json
import logging
import logging.handlers
import os
import re
import shutil
import socketserver
import sys
import threading
import time
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qsl

from PIL import Image

import pipeline
from config import ACTIONS, ConfigError, load_config, parse

ROOT = Path(__file__).resolve().parent
WEB = ROOT / "web"
FRAME_ID = re.compile(r"^\d{8}-\d{6}(-\d{3})?$")
MAX_UPLOAD = 50 * 1024 * 1024
# Names this server answers to. Anything else is refused: a page on another site can't write to it
# (Origin) or read frames through a DNS name pointed at 127.0.0.1 (Host). MIGAKU_ALLOWED_HOSTS
# (comma-separated) adds names, e.g. for a server reached from another machine.
LOCAL_HOSTS = {"localhost", "127.0.0.1", "[::1]", "::1"}
log = logging.getLogger("server")


class IsoFormatter(logging.Formatter):
    """ISO 8601 timestamps with milliseconds and UTC offset, so host and server logs line up."""

    def formatTime(self, record, datefmt=None):
        return datetime.fromtimestamp(record.created).astimezone().isoformat(timespec="milliseconds")


def setup_logging(data: Path) -> Path:
    """stdout (docker compose logs) and data/logs/server.log (readable from the host's ./data).
    MIGAKU_LOG_LEVEL=DEBUG also logs every GET, including the viewer's polling."""
    level = getattr(logging, os.environ.get("MIGAKU_LOG_LEVEL", "INFO").upper(), logging.INFO)
    fmt = IsoFormatter("%(asctime)s %(levelname)-7s %(name)s: %(message)s")
    root = logging.getLogger()
    root.setLevel(level)
    console = logging.StreamHandler(sys.stdout)
    console.setFormatter(fmt)
    root.addHandler(console)
    path = data / "logs" / "server.log"
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        fh = logging.handlers.RotatingFileHandler(path, maxBytes=5_000_000, backupCount=3, encoding="utf-8")
        fh.setFormatter(fmt)
        root.addHandler(fh)
    except OSError as e:
        log.warning("can't write %s: %s", path, e)
    return path
PRUNE_EVERY = 600  # seconds


def write_json(path: Path, obj) -> None:
    """Write then rename, so a reader never sees half a file."""
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(obj, ensure_ascii=False, indent=2))
    tmp.replace(path)


class Store:
    """Frames live in <data>/frames/<id>/{shot.png, ocr.json[, pinned]}."""

    def __init__(self, data: Path, engine: str, config_path: Path):
        self.data = data
        self.frames = data / "frames"
        self.frames.mkdir(parents=True, exist_ok=True)
        self.default_engine = engine
        self.config_path = config_path
        self.id_lock = threading.Lock()
        self.ocr_lock = threading.Lock()  # one OCR process at a time
        self.pending = {}  # frame id -> Event, set when its OCR has finished
        self.summaries = {}  # frame id -> the parts of its list() entry that come from ocr.json
        # Newest frame, and the live viewer's state; the condition wakes waiting viewers.
        self.cond = threading.Condition()
        self.latest = self.newest()
        self.live = {"seen": 0.0, "waiting": 0, "focused": False, "frame": None, "shown": False}
        # The declarative config. A file that fails to load at start leaves the defaults in
        # place and the error on show in the web UI; a failed reload keeps the previous config.
        self.config, self.config_warnings, self.config_error = parse(None)[0], [], None
        self.config_loaded = 0.0
        try:
            self.reload_config()
        except ConfigError as e:
            self.config_error = str(e)
            log.error("config: %s (using defaults)", e)

    # --- config ---------------------------------------------------------------------
    def reload_config(self) -> None:
        """Re-read the config file; on error (ConfigError) the current config stays."""
        config, warnings = load_config(self.config_path)
        self.config, self.config_warnings, self.config_error = config, warnings, None
        self.config_loaded = time.time()
        log.info("config: loaded %s", self.config_path)
        for w in warnings:
            log.warning("config: %s", w)

    @property
    def engine(self) -> str:
        return self.config["ocr_engine"] or self.default_engine

    def retention_hours(self) -> float:
        return self.config["retention_hours"]

    def config_state(self) -> dict:
        return {**self.config, "engine": self.engine, "engines": list(pipeline.AVAILABLE),
                "actions": {name: about for name, (_, about) in ACTIONS.items()},
                "path": str(self.config_path), "loaded": self.config_loaded,
                "warnings": self.config_warnings, "error": self.config_error}

    # --- frames ---------------------------------------------------------------------
    def path(self, frame_id: str) -> Path:
        if not FRAME_ID.match(frame_id):
            raise KeyError(frame_id)
        p = self.frames / frame_id
        if not p.is_dir():
            raise KeyError(frame_id)
        return p

    def frame_dirs(self) -> list:
        return [f for f in self.frames.iterdir() if f.is_dir() and FRAME_ID.match(f.name)]

    def newest(self):
        return max((f.name for f in self.frame_dirs()), default=None)  # ids sort by time

    def new_frame_dir(self) -> tuple:
        with self.id_lock:  # one id per millisecond
            while True:
                now = time.time()
                frame_id = time.strftime("%Y%m%d-%H%M%S", time.localtime(now)) + f"-{int(now * 1000) % 1000:03d}"
                try:
                    (self.frames / frame_id).mkdir()
                    return frame_id, now
                except FileExistsError:
                    time.sleep(0.001)

    def add(self, image_bytes: bytes, engine: str, game: str = "", wait: bool = True) -> dict:
        """Save the picture and OCR it in the background. With wait=False, return straight away
        (the live viewer shows the picture at once); otherwise wait for the text."""
        frame_id, now = self.new_frame_dir()
        frame = self.frames / frame_id
        try:
            image = Image.open(io.BytesIO(image_bytes))
            if image.format == "PNG":
                (frame / "shot.png").write_bytes(image_bytes)  # as is: re-encoding a 4K PNG is slow
            else:
                image.convert("RGB").save(frame / "shot.png", "PNG")
        except Exception:
            shutil.rmtree(frame, ignore_errors=True)
            raise ValueError("upload is not an image") from None
        self.pending[frame_id] = threading.Event()
        meta = {"created": now, **({"game": game} if game else {})}
        threading.Thread(target=self.run_ocr, args=(frame_id, engine, meta), daemon=True).start()
        if not wait:
            self.set_latest(frame_id)
            return {"id": frame_id, "lines": None}
        data = self.ocr_result(frame_id)
        if "error" in data:  # a blocking caller gets the error instead of a frame without text
            self.delete(frame_id)
            raise pipeline.OcrError(data["error"])
        self.set_latest(frame_id)
        return {"id": frame_id, "lines": [l["text"] for l in data["lines"]]}

    def run_ocr(self, frame_id: str, engine: str, meta: dict) -> None:
        frame = self.frames / frame_id
        start = time.monotonic()
        try:
            with self.ocr_lock:
                waited = time.monotonic() - start
                data = pipeline.ocr(frame / "shot.png", engine)
            log.info("ocr %s: %s, %d lines in %d blocks, %d ms (+%d ms queued) %s", frame_id, engine, len(data["lines"]),
                     len(data["blocks"]), (time.monotonic() - start - waited) * 1000, waited * 1000,
                     [l["text"] for l in data["lines"]][:6])
        except Exception as e:
            data = {"engine": engine, "lines": [], "blocks": [], "error": str(e)}
            log.exception("ocr %s (%s) failed", frame_id, engine)
        try:
            write_json(frame / "ocr.json", {**data, **meta})
        except OSError as e:  # e.g. the frame was deleted while its OCR ran
            log.warning("ocr %s: can't save the result: %s", frame_id, e)
        finally:
            self.pending.pop(frame_id).set()  # always, so nothing waits on it for nothing

    def ocr_result(self, frame_id: str) -> dict:
        frame = self.path(frame_id)
        done = self.pending.get(frame_id)
        if done and not done.wait(120):
            return {"lines": [], "blocks": [], "error": "OCR is still running after 120s"}
        try:
            return json.loads((frame / "ocr.json").read_text())
        except FileNotFoundError:
            raise KeyError(frame_id) from None  # deleted meanwhile

    # --- live viewer ----------------------------------------------------------------
    def set_latest(self, frame_id) -> None:
        with self.cond:
            self.latest = frame_id
            self.cond.notify_all()

    def report_live(self, body: dict) -> None:
        """The viewer reports focus/visibility/frame; the hotkey client reports shown. A viewer
        that's been minimised or hidden isn't shown any more, however it got there."""
        before = dict(self.live)
        if "focused" in body:
            self.live["focused"] = bool(body["focused"])
        if "frame" in body:
            self.live["frame"] = body["frame"] or None
        if "shown" in body:
            self.live["shown"] = bool(body["shown"])
        if body.get("visible") is False:
            self.live["shown"] = False
        if "visible" in body or "focused" in body:
            self.live["seen"] = time.time()
        changed = {k: v for k, v in self.live.items() if before.get(k) != v and k != "seen"}
        if changed:
            log.info("live: %s (from %s)", changed, body)

    def wait_latest(self, after: str, timeout: float = 25):
        with self.cond:
            self.live["waiting"] += 1
            try:
                self.cond.wait_for(lambda: (self.latest or "") != after, timeout)
            finally:
                self.live["waiting"] -= 1
                self.live["seen"] = time.time()
            return self.latest

    def live_state(self) -> dict:
        # Open while it has a request waiting here, or reported in the last few seconds.
        is_open = self.live["waiting"] > 0 or time.time() - self.live["seen"] < 5
        return {"open": is_open, "focused": is_open and self.live["focused"], "frame": self.live["frame"],
                "shown": is_open and self.live["shown"]}

    # --- listing --------------------------------------------------------------------
    def ocr_summary(self, frame: Path) -> dict:
        """The parts of a frame's summary that come from its ocr.json, cached once OCR is done
        (the file never changes after that)."""
        cached = self.summaries.get(frame.name)
        if cached:
            return cached
        try:
            data = json.loads((frame / "ocr.json").read_text())
        except (OSError, ValueError):
            data = {}
        blocks = [" ".join(data["lines"][i]["text"] for i in b) for b in data.get("blocks", [])]
        # Preview: the first block that reads like dialogue, not a name tag.
        preview = next((b for b in blocks if len(b) > 12 or re.search(r"[「。、！？…]", b)), blocks[0] if blocks else "")
        summary = {
            "id": frame.name,
            "created": data.get("created") or frame.stat().st_mtime,
            "engine": data.get("engine", "vision"),
            "game": data.get("game", ""),
            "lines": len(data.get("lines", [])),
            "preview": preview,
        }
        if data and frame.name not in self.pending:
            self.summaries[frame.name] = summary
        return summary

    def list(self) -> list:
        hours = self.retention_hours()
        out = []
        for frame in self.frame_dirs():
            s = dict(self.ocr_summary(frame))
            s["pinned"] = (frame / "pinned").exists()
            s["expires"] = None if s["pinned"] or hours == 0 else s["created"] + hours * 3600
            out.append(s)
        return sorted(out, key=lambda s: s["created"], reverse=True)

    def pin(self, frame_id: str, pinned: bool) -> None:
        marker = self.path(frame_id) / "pinned"
        if pinned:
            marker.touch()
        else:
            marker.unlink(missing_ok=True)

    def delete(self, frame_id: str) -> None:
        shutil.rmtree(self.path(frame_id))
        self.summaries.pop(frame_id, None)
        self.set_latest(self.newest())

    def prune(self) -> int:
        now = time.time()
        expired = [s["id"] for s in self.list() if s["expires"] is not None and s["expires"] < now]
        for frame_id in expired:
            shutil.rmtree(self.frames / frame_id, ignore_errors=True)
            self.summaries.pop(frame_id, None)
        if expired:
            self.set_latest(self.newest())
            log.info("retention: removed %d frame(s)", len(expired))
        return len(expired)


class Words:
    """The word matcher for coloured translations (align.py), loaded on first use: Janome and the
    JMdict table come with the container image, or with a native install (tools/native.sh builds
    the table into build/); MIGAKU_JMDICT names it."""

    def __init__(self):
        self.aligner, self.error, self.lock = None, None, threading.Lock()

    def align(self, ja: str, en: str) -> dict:
        with self.lock:
            if self.aligner is None and self.error is None:
                try:
                    from align import Aligner
                    path = Path(os.environ.get("MIGAKU_JMDICT") or ROOT / "build" / "jmdict.sqlite")
                    if not path.exists():
                        raise FileNotFoundError(f"no JMdict table at {path} (tools/build_jmdict.py)")
                    self.aligner = Aligner(path)
                except Exception as e:  # ImportError (no Janome) or the missing table
                    self.error = f"word matching unavailable: {e}"
                    log.warning("%s", self.error)
            if self.error:
                return {"pairs": [], "error": self.error}
            return {"pairs": self.aligner.align(ja, en)}


class Handler(SimpleHTTPRequestHandler):
    store: Store
    words = Words()

    # --- helpers --------------------------------------------------------------------
    def send_json(self, obj, status: int = 200) -> None:
        body = json.dumps(obj, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")  # live state, never cached
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def read_body(self) -> bytes:
        length = int(self.headers.get("Content-Length") or 0)
        if length > MAX_UPLOAD:
            raise ValueError("upload too large")
        return self.rfile.read(length)

    def route(self):
        path, _, query = self.path.partition("?")
        params = dict(parse_qsl(query))
        return path.rstrip("/") or "/", params

    def handle_errors(fn):
        def wrapper(self):
            try:
                fn(self)
            except KeyError:
                self.send_json({"error": "frame not found"}, 404)
            except (ValueError, pipeline.OcrError) as e:
                log.warning("%s %s: %s", self.command, self.path, e)
                self.send_json({"error": str(e)}, 400)
            except Exception as e:  # keep the server up; report to the client
                log.exception("%s %s failed", self.command, self.path)
                self.send_json({"error": f"{type(e).__name__}: {e}"}, 500)
        return wrapper

    # --- routes ---------------------------------------------------------------------
    def do_GET(self) -> None:
        path, params = self.route()
        if path == "/api/config":
            return self.send_json(self.store.config_state())
        if path == "/api/frames":
            return self.send_json(self.store.list())
        if path == "/api/live":
            return self.send_json(self.store.live_state())
        if path == "/api/latest":
            return self.send_json({"id": self.store.wait_latest(params.get("after", ""))})
        m = re.match(r"^/api/frames/([^/]+)/ocr$", path)
        if m:
            try:
                return self.send_json(self.store.ocr_result(m.group(1)))
            except (KeyError, OSError):
                return self.send_json({"error": "frame not found"}, 404)
        if path.startswith("/frames/"):
            # Frame files come from the data dir, not web/.
            parts = path.split("/")
            if len(parts) == 4 and FRAME_ID.match(parts[2]) and parts[3] in ("shot.png", "ocr.json"):
                self.path = "/" + parts[2] + "/" + parts[3]
                self.directory = str(self.store.frames)
                return super().do_GET()
            return self.send_error(404)
        return super().do_GET()

    @handle_errors
    def do_POST(self) -> None:
        path, params = self.route()
        if path == "/api/frames":
            engine = params.get("ocr") or self.store.engine
            result = self.store.add(self.read_body(), engine, params.get("game", "")[:100],
                                    wait=params.get("wait") != "0")
            result["url"] = f"/viewer.html?frame={result['id']}"
            return self.send_json(result, 201)
        if path == "/api/config/reload":
            try:
                self.store.reload_config()
            except ConfigError as e:
                self.store.config_error = str(e)
                return self.send_json({**self.store.config_state(), "error": str(e)}, 400)
            self.store.prune()  # retention may have changed
            return self.send_json(self.store.config_state())
        if path == "/api/live":
            self.store.report_live(json.loads(self.read_body() or b"{}"))
            self.send_response(204)
            return self.end_headers()
        if path == "/api/align":
            body = json.loads(self.read_body() or b"{}")
            return self.send_json(self.words.align(str(body.get("ja", ""))[:1000], str(body.get("en", ""))[:2000]))
        if path == "/api/log":
            logging.getLogger("viewer").info("%s", self.read_body()[:2000].decode(errors="replace"))
            self.send_response(204)
            return self.end_headers()
        if path == "/debug":
            debug = self.store.data / "debug"
            debug.mkdir(exist_ok=True)
            (debug / "dom.html").write_bytes(self.read_body())
            self.send_response(204)
            return self.end_headers()
        self.send_error(404)

    @handle_errors
    def do_PUT(self) -> None:
        path, _ = self.route()
        m = re.match(r"^/api/frames/([^/]+)/pin$", path)
        if m:
            self.store.pin(m.group(1), bool(json.loads(self.read_body() or b"{}").get("pinned", True)))
            return self.send_json({"ok": True})
        self.send_error(404)

    @handle_errors
    def do_DELETE(self) -> None:
        path, _ = self.route()
        m = re.match(r"^/api/frames/([^/]+)$", path)
        if m:
            self.store.delete(m.group(1))
            return self.send_json({"ok": True})
        self.send_error(404)

    allowed_hosts = LOCAL_HOSTS

    def parse_request(self):
        self.started = time.monotonic()
        if not super().parse_request():
            return False
        host = hostname(self.headers.get("Host", ""))
        # Origin only matters for writes: reads are already same-origin only (no CORS headers), and
        # extensions (Migaku taking the picture for a card) fetch frames with their own origin.
        origin = self.headers.get("Origin") if self.command not in ("GET", "HEAD") else None
        if host not in self.allowed_hosts or (origin and hostname(origin) not in self.allowed_hosts):
            log.warning("refused %s %s: Host %r Origin %r", self.command, self.path[:200], self.headers.get("Host"), origin)
            self.send_error(403, "this server only answers to localhost (MIGAKU_ALLOWED_HOSTS adds names)")
            return False
        return True

    def log_request(self, code="-", size="-") -> None:
        # API writes and errors at INFO; GETs (pages, polling, long-polls) only at DEBUG.
        status = getattr(code, "value", code)
        ms = (time.monotonic() - getattr(self, "started", time.monotonic())) * 1000
        level = logging.INFO if self.command != "GET" or str(status).startswith(("4", "5")) else logging.DEBUG
        log.log(level, "%s %s -> %s (%d ms)", self.command, self.path[:200], status, ms)

    def log_message(self, fmt, *args) -> None:  # anything else BaseHTTPRequestHandler reports
        log.warning("http: " + fmt, *args)


def hostname(value: str) -> str:
    """The host part of a Host header or an Origin URL, lowercased, without the port."""
    value = value.strip().lower().split("://", 1)[-1].split("/", 1)[0]
    if value.startswith("["):  # [::1]:8765
        return value.split("]", 1)[0] + "]"
    return value.rsplit(":", 1)[0] if value.count(":") == 1 else value


class Server(ThreadingHTTPServer):
    def server_bind(self) -> None:
        # HTTPServer.server_bind looks up the host's FQDN, only for a name nothing here uses; on macOS
        # that reverse lookup can stall startup for ~30s.
        socketserver.TCPServer.server_bind(self)
        self.server_name, self.server_port = self.server_address[:2]


def prune_loop(store: Store) -> None:
    while True:
        try:
            store.prune()
        except Exception as e:
            log.exception("retention: %s", e)
        time.sleep(PRUNE_EVERY)


def main() -> None:
    env = os.environ.get
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument("--ocr", choices=pipeline.AVAILABLE, default=env("MIGAKU_OCR", pipeline.DEFAULT_OCR))
    p.add_argument("--host", default=env("MIGAKU_HOST", "127.0.0.1"))
    p.add_argument("--port", type=int, default=int(env("MIGAKU_PORT", "8765")))
    p.add_argument("--data", type=Path, default=Path(env("MIGAKU_DATA", str(ROOT / "data"))))
    p.add_argument("--config", type=Path, default=Path(env("MIGAKU_CONFIG", str(ROOT / "config" / "config.yaml"))),
                   help="the YAML config file (retention, profiles, keybindings, ...)")
    args = p.parse_args()

    log_path = setup_logging(args.data.resolve())
    log.info("starting: python %s, data=%s, log=%s, level=%s", sys.version.split()[0], args.data.resolve(), log_path,
             logging.getLevelName(logging.getLogger().level))
    store = Store(args.data.resolve(), args.ocr, args.config)
    Handler.store = store
    extra = {h.strip().lower() for h in env("MIGAKU_ALLOWED_HOSTS", "").split(",") if h.strip()}
    Handler.allowed_hosts = LOCAL_HOSTS | extra
    threading.Thread(target=prune_loop, args=(store,), daemon=True).start()
    server = Server((args.host, args.port), partial(Handler, directory=str(WEB)))
    log.info("serving on http://%s:%d  ocr=%s  config=%s  retention=%gh", args.host, args.port, store.engine,
             args.config, store.retention_hours())
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
