"""End-to-end check of a running frame server (stdlib only). CI runs it against the built image.

    python3 tests/smoke_test.py [http://localhost:8765 [CONFIG_DIR]]

Uploads samples/ff8_dialogue.png (synthetic, from make_test_image.py), checks the OCR text, the
async upload path, the live-viewer endpoints and the config, then deletes what it made. Given
CONFIG_DIR (the folder mounted at /config in the container), it also checks config reloads:
it writes config.yaml there, so point it at a throwaway container, never your real config.
"""
import json
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

SERVER = (sys.argv[1] if len(sys.argv) > 1 else "http://localhost:8765").rstrip("/")
CONFIG_DIR = Path(sys.argv[2]) if len(sys.argv) > 2 else None
SAMPLE = Path(__file__).resolve().parent.parent / "samples" / "ff8_dialogue.png"


def call(method, path, body=None, expect=200):
    data = body if isinstance(body, bytes) or body is None else json.dumps(body).encode()
    req = urllib.request.Request(SERVER + path, data=data, method=method)
    try:
        with urllib.request.urlopen(req, timeout=120) as resp:
            status, raw = resp.status, resp.read()
    except urllib.error.HTTPError as e:
        status, raw = e.code, e.read()
    assert status == expect, f"{method} {path}: HTTP {status}, expected {expect}: {raw[:200]!r}"
    return json.loads(raw) if raw[:1] in (b"{", b"[") else raw


def wait_for_server():
    for _ in range(60):
        try:
            return call("GET", "/api/config")
        except (urllib.error.URLError, ConnectionError, AssertionError):
            time.sleep(1)
    sys.exit(f"no server at {SERVER}")


def main():
    cfg = wait_for_server()
    assert "meiki" in cfg["engines"], cfg
    assert cfg["error"] is None and cfg["keybindings"]["translate"], cfg
    created = []
    try:
        for page in ("/", "/viewer.html", "/settings.html", "/picture.html"):
            call("GET", page)

        # Blocking upload: OCR text comes back.
        frame = call("POST", "/api/frames?game=smoke", SAMPLE.read_bytes(), 201)
        created.append(frame["id"])
        text = "".join(frame["lines"])
        assert "リノア" in text and "心配" in text, f"unexpected OCR text: {frame['lines']}"
        print(f"ok   OCR: {frame['lines']}")

        # Async upload: returns before OCR; the OCR endpoint waits for it.
        t = time.time()
        frame = call("POST", "/api/frames?wait=0", SAMPLE.read_bytes(), 201)
        created.append(frame["id"])
        assert frame["lines"] is None
        ocr = call("GET", f"/api/frames/{frame['id']}/ocr")
        assert ocr["lines"] and "error" not in ocr, ocr
        print(f"ok   async upload + OCR wait ({time.time() - t:.1f}s)")

        assert call("GET", "/api/latest?after=x")["id"] == frame["id"]
        assert set(call("GET", "/api/live")) == {"open", "focused", "frame", "shown"}
        # The hotkey marks the overlay shown; the viewer being minimised/hidden clears it.
        call("POST", "/api/live", {"focused": True, "visible": True}, 204)
        call("POST", "/api/live", {"shown": True}, 204)
        assert call("GET", "/api/live")["shown"] is True
        call("POST", "/api/live", {"focused": False, "visible": False}, 204)
        assert call("GET", "/api/live")["shown"] is False
        listed = {f["id"]: f for f in call("GET", "/api/frames")}
        assert listed[created[0]]["game"] == "smoke"
        print("ok   latest / live / list")

        call("POST", "/api/frames", b"not an image", 400)
        call("GET", "/api/frames/20000101-000000/ocr", expect=404)

        if CONFIG_DIR:
            check_reload()
    finally:
        for frame_id in created:
            call("DELETE", f"/api/frames/{frame_id}")
    print("all smoke tests passed")


def check_reload():
    """A valid file applies on reload; an invalid one is rejected and the previous config stays."""
    path = CONFIG_DIR / "config.yaml"
    path.write_text("active_profile: smoke\nprofiles:\n  smoke: {name: Smoke, freeze: true}\n"
                    "keybindings: {translate: j}\nretention_hours: 0\n")
    cfg = call("POST", "/api/config/reload")
    assert cfg["active_profile"] == "smoke" and cfg["profiles"]["smoke"]["freeze"] is True, cfg
    assert cfg["keybindings"]["translate"] == "j" and cfg["retention_hours"] == 0, cfg

    path.write_text("active_profile: missing\n")
    bad = call("POST", "/api/config/reload", expect=400)
    assert "missing" in bad["error"] and bad["active_profile"] == "smoke", bad
    assert call("GET", "/api/config")["error"], "the error stays visible until a good reload"

    path.write_text("")
    cfg = call("POST", "/api/config/reload")
    assert cfg["error"] is None and cfg["profiles"] == {}, cfg
    print("ok   config reload")


if __name__ == "__main__":
    main()
