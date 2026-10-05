# migaku-games (POC)

Freeze-frame OCR for games: screenshot → Japanese OCR → a local page in Brave where the
Migaku extension handles hover lookups, card creation and translation.

Two parts:

- **Frame server** (`server.py`, runs in Docker): web UI, OCR (meikiocr), frame storage and
  retention. http://localhost:8765
- **Capture client** (`migaku_games.py`, runs on the host, stdlib only): takes the screenshot,
  copies it to the clipboard, uploads it to the server and opens the viewer in Brave. Starts the
  container with `docker compose up -d` if it isn't running.

```sh
docker compose up -d                     # build + start the server (podman compose works too)
python3 migaku_games.py                  # drag-select a screen region
python3 migaku_games.py --full           # whole screen
python3 migaku_games.py --image x.png    # use an existing image
python3 migaku_games.py --app            # chromeless Brave window
python3 make_test_image.py               # synthetic samples into samples/
```

## Web UI

- `/` lists frames (newest first, refreshes itself): open, pin, delete, or drop/paste an image
  to OCR it. Retention is set here too.
- The viewer's toolbar: frame navigation, translate (all boxes, or `y` over one, via Migaku's
  translator), OCR text and transcript toggles, caption size. `?` lists every shortcut,
  including Migaku's (`E` over the picture sends it to the card creator).

`web/` is mounted into the container, so UI edits show up on reload; Python changes need
`docker compose up -d --build`.

## Retention

Unpinned frames are deleted after `MIGAKU_RETENTION_HOURS` (24 by default, in `compose.yaml`);
0 keeps everything. Changing it in the web UI saves it to `data/settings.json`, which wins from
then on. Pinned frames are never deleted. Frames live in `./data/frames`.

## OCR engines

Both print the same JSON (`{width, height, lines: [{text, conf, x, y, w, h}]}`):

- `meiki`: [meikiocr](https://github.com/rtr46/meikiocr) (`ocr/meiki_ocr.py`), open source and
  trained on game text. The container's engine; its models are baked into the image.
- `vision`: Apple's Vision framework (`ocr/vision_ocr.swift`). macOS only, so it needs the server
  run natively (`python3 server.py --ocr vision`); kept for comparison, not used by default.

On Linux, capture uses `spectacle` (KDE) and the clipboard uses `wl-copy`.
