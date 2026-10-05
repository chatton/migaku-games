"""Frame server: web UI, OCR, frame storage and retention.

    python3 server.py                       # http://localhost:8765
    python3 server.py --ocr meiki --data ./data --config ./config/config.yaml

The options can also come from the environment (what the container uses):
MIGAKU_OCR, MIGAKU_HOST, MIGAKU_PORT, MIGAKU_DATA, MIGAKU_CONFIG.

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
  POST   /api/log                        a line from a page for the server log (e.g. clipboard copies)
  POST   /debug                          viewer posts its DOM here (dev aid)
"""
import argparse
import io
import json
import os
import re
import shutil
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
        self.live = {"seen": 0.0, "waiting": 0, "focused": False, "frame": None}
        # The declarative config. A file that fails to load at start leaves the defaults in
        # place and the error on show in the web UI; a failed reload keeps the previous config.
        self.config, self.config_warnings, self.config_error = parse(None)[0], [], None
        self.config_loaded = 0.0
        try:
            self.reload_config()
        except ConfigError as e:
            self.config_error = str(e)
            print(f"config: {e}", file=sys.stderr, flush=True)

    # --- config ---------------------------------------------------------------------
    def reload_config(self) -> None:
        """Re-read the config file; on error (ConfigError) the current config stays."""
        config, warnings = load_config(self.config_path)
        self.config, self.config_warnings, self.config_error = config, warnings, None
        self.config_loaded = time.time()
        for w in warnings:
            print(f"config: {w}", flush=True)

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
        try:
            with self.ocr_lock:
                data = pipeline.ocr(frame / "shot.png", engine)
        except Exception as e:
            data = {"engine": engine, "lines": [], "blocks": [], "error": str(e)}
            print(f"ocr {frame_id}: {e}", file=sys.stderr, flush=True)
        write_json(frame / "ocr.json", {**data, **meta})
        self.pending.pop(frame_id).set()

    def ocr_result(self, frame_id: str) -> dict:
        frame = self.path(frame_id)
        done = self.pending.get(frame_id)
        if done:
            done.wait(120)
        return json.loads((frame / "ocr.json").read_text())

    # --- live viewer ----------------------------------------------------------------
    def set_latest(self, frame_id) -> None:
        with self.cond:
            self.latest = frame_id
            self.cond.notify_all()

    def report_live(self, focused, frame) -> None:
        self.live.update(seen=time.time(), focused=bool(focused), frame=frame or None)

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
        return {"open": is_open, "focused": is_open and self.live["focused"], "frame": self.live["frame"]}

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
            print(f"retention: removed {len(expired)} frame(s)", flush=True)
        return len(expired)


class Handler(SimpleHTTPRequestHandler):
    store: Store

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
                self.send_json({"error": str(e)}, 400)
            except Exception as e:  # keep the server up; report to the client
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
            body = json.loads(self.read_body() or b"{}")
            self.store.report_live(body.get("focused"), body.get("frame"))
            self.send_response(204)
            return self.end_headers()
        if path == "/api/log":
            print(f"page: {self.read_body()[:500].decode(errors='replace')}", flush=True)
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

    def log_message(self, fmt, *args) -> None:
        # Skip the noisy static/polling requests; keep API writes and errors.
        if self.command == "GET" and not str(args[1] if len(args) > 1 else "").startswith(("4", "5")):
            return
        super().log_message(fmt, *args)


def prune_loop(store: Store) -> None:
    while True:
        try:
            store.prune()
        except Exception as e:
            print(f"retention: {e}", file=sys.stderr, flush=True)
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

    store = Store(args.data.resolve(), args.ocr, args.config)
    Handler.store = store
    threading.Thread(target=prune_loop, args=(store,), daemon=True).start()
    server = ThreadingHTTPServer((args.host, args.port), partial(Handler, directory=str(WEB)))
    print(f"serving on http://{args.host}:{args.port}  ocr={store.engine}  data={store.data}  "
          f"config={args.config}  retention={store.retention_hours():g}h", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
