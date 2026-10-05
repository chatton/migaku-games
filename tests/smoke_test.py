"""End-to-end check of a running frame server (stdlib only). CI runs it against the built image.

    python3 tests/smoke_test.py [http://localhost:8765]

Uploads samples/ff8_dialogue.png (synthetic, from make_test_image.py), checks the OCR text, the
async upload path, the live-viewer endpoints and settings validation, then deletes what it made
and restores the settings.
"""
import json
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

SERVER = (sys.argv[1] if len(sys.argv) > 1 else "http://localhost:8765").rstrip("/")
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
    ctype = "json" if raw[:1] in (b"{", b"[") else "raw"
    return json.loads(raw) if ctype == "json" else raw


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
        assert set(call("GET", "/api/live")) == {"open", "focused", "frame"}
        listed = {f["id"]: f for f in call("GET", "/api/frames")}
        assert listed[created[0]]["game"] == "smoke"
        print("ok   latest / live / list")

        call("POST", "/api/frames", b"not an image", 400)
        call("GET", "/api/frames/20000101-000000/ocr", expect=404)

        # Settings: a valid profile saves; invalid input is rejected.
        call("PUT", "/api/config", {"profiles": {"psmoke": {"name": "Smoke", "freeze": True}}})
        cfg2 = call("PUT", "/api/config", {"active_profile": "psmoke"})
        assert cfg2["active_profile"] == "psmoke" and cfg2["profiles"]["psmoke"]["freeze"] is True
        call("PUT", "/api/config", {"active_profile": "missing"}, 400)
        call("PUT", "/api/config", {"profiles": {"x": {"name": ""}}}, 400)
        call("PUT", "/api/config", {"retention_hours": -1}, 400)
        print("ok   settings")
    finally:
        for frame_id in created:
            call("DELETE", f"/api/frames/{frame_id}")
        call("PUT", "/api/config", {"profiles": cfg["profiles"], "active_profile": cfg["active_profile"]})
    print("all smoke tests passed")


if __name__ == "__main__":
    main()
