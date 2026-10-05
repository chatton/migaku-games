"""Frame server: web UI, OCR, frame storage and retention.

    python3 server.py                       # http://localhost:8765
    python3 server.py --ocr meiki --retention-hours 48 --data ./data

Every option can also come from the environment (what the container uses):
MIGAKU_OCR, MIGAKU_HOST, MIGAKU_PORT, MIGAKU_DATA, MIGAKU_RETENTION_HOURS.

Retention set from the web UI is saved to <data>/settings.json and takes precedence over
the flag/env default from then on. Pinned frames are never pruned; 0 hours keeps everything.

API
  GET    /api/config                     engine, engines, retention_hours, profiles, active_profile
  PUT    /api/config                     any of {"retention_hours": N,
                                           "profiles": {id: {"name", "freeze", "process"}},
                                           "active_profile": id | null}
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

ROOT = Path(__file__).resolve().parent
WEB = ROOT / "web"
FRAME_ID = re.compile(r"^\d{8}-\d{6}(-\d{3})?$")
MAX_UPLOAD = 50 * 1024 * 1024
PRUNE_EVERY = 600  # seconds


class Store:
    """Frames live in <data>/frames/<id>/{shot.png, ocr.json[, pinned]}."""

    def __init__(self, data: Path, engine: str, retention_hours: float):
        self.data = data
        self.frames = data / "frames"
        self.frames.mkdir(parents=True, exist_ok=True)
        self.settings_path = data / "settings.json"
        self.engine = engine
        self.default_retention = retention_hours
        self.id_lock = threading.Lock()
        self.settings_lock = threading.Lock()
        self.ocr_lock = threading.Lock()  # one OCR process at a time
        self.pending = {}  # frame id -> Event, set when its OCR has finished
        # Newest frame, and the live viewer's state; the condition wakes waiting viewers.
        self.cond = threading.Condition()
        self.latest = self.newest()
        self.live = {"seen": 0.0, "waiting": 0, "focused": False, "frame": None}

    # --- settings -------------------------------------------------------------------
    def settings(self) -> dict:
        try:
            return json.loads(self.settings_path.read_text())
        except (OSError, ValueError):
            return {}

    def save_settings(self, **changes) -> None:
        with self.settings_lock:
            settings = {**self.settings(), **changes}
            tmp = self.settings_path.with_suffix(".tmp")
            tmp.write_text(json.dumps(settings, ensure_ascii=False, indent=2))
            tmp.replace(self.settings_path)

    def retention_hours(self) -> float:
        try:
            return float(self.settings()["retention_hours"])
        except (KeyError, ValueError, TypeError):
            return self.default_retention

    def set_retention_hours(self, hours: float) -> None:
        if hours < 0:
            raise ValueError("retention_hours must be >= 0")
        self.save_settings(retention_hours=hours)

    # Game profiles, made in the settings page. One is active; the capture client tags frames
    # with its name and takes the experimental freeze settings from it. Ids are stable, so a
    # profile can be renamed.
    def profiles(self) -> dict:
        return self.settings().get("profiles", {})

    def active_profile(self):
        active = self.settings().get("active_profile")
        return active if active in self.profiles() else None

    def set_profiles(self, profiles) -> None:
        if not isinstance(profiles, dict):
            raise ValueError("profiles must be an object")
        clean = {}
        for pid, prof in profiles.items():
            if not (isinstance(prof, dict) and re.fullmatch(r"[\w-]{1,40}", str(pid))):
                raise ValueError(f"bad profile {pid!r}")
            name = str(prof.get("name") or "").strip()[:100]
            if not name:
                raise ValueError("a profile needs a name")
            clean[pid] = {"name": name, "freeze": bool(prof.get("freeze")),
                          "process": str(prof.get("process") or "").strip()[:100]}
        self.save_settings(profiles=clean)

    def set_active_profile(self, pid) -> None:
        if pid is not None and pid not in self.profiles():
            raise ValueError("no such profile")
        self.save_settings(active_profile=pid)

    # --- frames ---------------------------------------------------------------------
    def path(self, frame_id: str) -> Path:
        if not FRAME_ID.match(frame_id):
            raise KeyError(frame_id)
        p = self.frames / frame_id
        if not p.is_dir():
            raise KeyError(frame_id)
        return p

    def newest(self):
        ids = [f.name for f in self.frames.iterdir() if f.is_dir() and FRAME_ID.match(f.name)]
        return max(ids, default=None)  # ids sort by time

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
        """Save the picture and OCR it. With wait=False, return as soon as the picture is saved
        (the live viewer shows it straight away) and OCR in the background."""
        frame_id, now = self.new_frame_dir()
        frame = self.frames / frame_id
        try:
            image = Image.open(io.BytesIO(image_bytes))
            image.convert("RGB").save(frame / "shot.png", "PNG")
        except Exception:
            shutil.rmtree(frame, ignore_errors=True)
            raise ValueError("upload is not an image") from None
        done = self.pending[frame_id] = threading.Event()
        meta = {"created": now, **({"game": game} if game else {})}
        if not wait:
            self.set_latest(frame_id)
            threading.Thread(target=self.run_ocr, args=(frame_id, engine, meta), daemon=True).start()
            return {"id": frame_id, "lines": None}
        data = self.run_ocr(frame_id, engine, meta)
        if "error" in data:
            shutil.rmtree(frame, ignore_errors=True)
            raise pipeline.OcrError(data["error"])
        self.set_latest(frame_id)
        return {"id": frame_id, "lines": [l["text"] for l in data["lines"]]}

    def run_ocr(self, frame_id: str, engine: str, meta: dict) -> dict:
        frame = self.frames / frame_id
        try:
            with self.ocr_lock:
                data = pipeline.ocr(frame / "shot.png", engine)
        except Exception as e:
            data = {"engine": engine, "lines": [], "blocks": [], "error": str(e)}
            print(f"ocr {frame_id}: {e}", file=sys.stderr, flush=True)
        data.update(meta)
        # Written then renamed, so a viewer never reads half a file.
        tmp = frame / "ocr.json.tmp"
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2))
        tmp.replace(frame / "ocr.json")
        self.pending.pop(frame_id).set()
        return data

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

    def summary(self, frame: Path) -> dict:
        try:
            data = json.loads((frame / "ocr.json").read_text())
        except (OSError, ValueError):
            data = {}
        created = data.get("created") or frame.stat().st_mtime
        pinned = (frame / "pinned").exists()
        hours = self.retention_hours()
        blocks = [" ".join(data["lines"][i]["text"] for i in b) for b in data.get("blocks", [])]
        # Preview: the first block that reads like dialogue, not a name tag.
        preview = next((b for b in blocks if len(b) > 12 or re.search(r"[「。、！？…]", b)), blocks[0] if blocks else "")
        return {
            "id": frame.name,
            "created": created,
            "engine": data.get("engine", "vision"),
            "game": data.get("game", ""),
            "lines": len(data.get("lines", [])),
            "preview": preview,
            "pinned": pinned,
            "expires": None if pinned or hours == 0 else created + hours * 3600,
        }

    def list(self) -> list:
        frames = [f for f in self.frames.iterdir() if f.is_dir() and FRAME_ID.match(f.name)]
        return sorted((self.summary(f) for f in frames), key=lambda s: s["created"], reverse=True)

    def pin(self, frame_id: str, pinned: bool) -> None:
        marker = self.path(frame_id) / "pinned"
        if pinned:
            marker.touch()
        else:
            marker.unlink(missing_ok=True)

    def delete(self, frame_id: str) -> None:
        shutil.rmtree(self.path(frame_id))
        self.latest = self.newest()

    def prune(self) -> int:
        removed = 0
        now = time.time()
        for s in self.list():
            if s["expires"] is not None and s["expires"] < now:
                shutil.rmtree(self.frames / s["id"], ignore_errors=True)
                removed += 1
        if removed:
            self.latest = self.newest()
            print(f"retention: removed {removed} frame(s)", flush=True)
        return removed


class Handler(SimpleHTTPRequestHandler):
    store: Store

    # --- helpers --------------------------------------------------------------------
    def send_json(self, obj, status: int = 200) -> None:
        body = json.dumps(obj, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
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

    def config(self) -> dict:
        return {"engine": self.store.engine, "engines": list(pipeline.AVAILABLE),
                "retention_hours": self.store.retention_hours(), "profiles": self.store.profiles(),
                "active_profile": self.store.active_profile()}

    # --- routes ---------------------------------------------------------------------
    def do_GET(self) -> None:
        path, params = self.route()
        if path == "/api/config":
            return self.send_json(self.config())
        if path == "/api/frames":
            return self.send_json(self.store.list())
        if path == "/api/live":
            return self.send_json(self.store.live_state())
        if path == "/api/latest":
            if "focused" in params:
                self.store.report_live(params["focused"] == "1", params.get("frame"))
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
        if path == "/api/live":
            body = json.loads(self.read_body() or b"{}")
            self.store.report_live(body.get("focused"), body.get("frame"))
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
        if path == "/api/config":
            body = json.loads(self.read_body() or b"{}")
            if "retention_hours" in body:
                self.store.set_retention_hours(float(body["retention_hours"]))
                self.store.prune()
            if "profiles" in body:
                self.store.set_profiles(body["profiles"])
            if "active_profile" in body:
                self.store.set_active_profile(body["active_profile"])
            return self.send_json(self.config())
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
    p.add_argument("--retention-hours", type=float, default=float(env("MIGAKU_RETENTION_HOURS", "24")),
                   help="delete unpinned frames older than this (0 = keep forever)")
    args = p.parse_args()

    store = Store(args.data.resolve(), args.ocr, args.retention_hours)
    Handler.store = store
    threading.Thread(target=prune_loop, args=(store,), daemon=True).start()
    server = ThreadingHTTPServer((args.host, args.port), partial(Handler, directory=str(WEB)))
    print(f"serving on http://{args.host}:{args.port}  ocr={args.ocr}  data={store.data}  "
          f"retention={store.retention_hours():g}h", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
