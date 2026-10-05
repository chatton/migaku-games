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
docker compose up -d                     # server + Steam screenshot watcher (podman compose works too)
python3 migaku_games.py                  # drag-select a screen region
python3 migaku_games.py --full           # whole screen
python3 migaku_games.py --image x.png    # use an existing image
python3 migaku_games.py --app            # chromeless Brave window
python3 make_test_image.py               # synthetic samples into samples/
```

## Overlay (the main way to play)

Bind `python3 ~/migaku-games/migaku_games.py --overlay` to a global shortcut (KDE: System
Settings → Shortcuts → Add New → Command or Script; e.g. `Meta+J`). Then, in the game:

1. Press the key: the screen under the mouse is captured and shown fullscreen in a single
   long-lived Brave window ("Migaku Live"), lined up with the game. The picture is there at
   once; the hoverable text follows when OCR finishes (about a second).
2. Hover words, `y` to translate, `E` for cards.
3. Press the key again: the window minimises and you're back in the game.

The first press opens the window (`/viewer.html?live`); authorise Migaku on it once and leave it
open. It swaps frames in place, so Migaku stays active. Works for any game: Steam, GOG, emulators.
Run games borderless windowed so the overlay can cover them.

### Settings and game profiles

`/settings.html` (gear icon in the viewer, link in the gallery) holds frame retention and **game
profiles**. Make one per game and select it when you play it: overlay captures are tagged with its
name, and it carries the per-game options. Everything is saved in `data/settings.json`.

**Freeze the game (experimental, Linux):** per profile. While the overlay is up the game's
processes are paused with SIGSTOP and resumed with SIGCONT when you hide it. With no process name
set, the running Steam game is frozen (found by Steam's `reaper SteamLaunch` process); otherwise
the process whose executable name contains the given text (`duckstation`, `retroarch`, ...).
Not for online games. If a game is ever left frozen: `python3 migaku_games.py --resume`.

The window is raised and hidden with a one-off KWin script over `dbus-send` (KDE Plasma), or
AppleScript on macOS, which asks once to let the terminal control Brave.

## Steam screenshots

`docker compose up -d` also starts `steam-watcher`, which OCRs every new Steam screenshot (F12,
or a controller button mapped in Steam Input) and tags the frame with the game's name. Open
http://localhost:8765 to see them. Screenshots already there at start are skipped.

It reads Steam from `~/.local/share/Steam` (native Steam, as on Bazzite). Elsewhere, set
`STEAM_DIR` in a `.env` file next to `compose.yaml`:

```sh
STEAM_DIR=$HOME/.var/app/com.valvesoftware.Steam/.local/share/Steam   # Flatpak Steam
STEAM_DIR=$HOME/Library/Application Support/Steam                      # macOS
```

Games installed in another Steam library show as `app <id>`, since only `STEAM_DIR` is mounted.
`steam_watcher.py` also runs on the host (`--open` opens each frame in Brave, `--dir` watches any
folder); don't run it alongside the container's or every screenshot is uploaded twice.

With rootless podman, `systemctl --user enable podman-restart` brings the containers back after
a reboot.

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

## Stretch goals

Ideas borrowed from other mining tools (GameSentenceMiner, YomiNinja, Game2Text), not built yet:

- **Capture area per profile:** OCR only the dialogue box, dropping watermark and menu noise.
- **Texthooking for emulators:** exact text from emulated games (e.g. Agent/Frida scripts), pushed
  into the live window instead of OCR. Migaku only needs text on the page.
- **More OCR engines:** manga-ocr (via owocr) next to meikiocr, selectable per profile.

**Sentence audio: investigated, not pursued.** Migaku's automatic sentence audio (tab recording,
Alt+R) is gated to an allowlist of video sites (Netflix, YouTube, Crunchyroll, ...) and its own
local video player; `E` on a page only sends text or an `<img>`, never `<audio>`, and a page can't
hand audio to the card creator. The only generic route is manual: paste, drop or upload an audio
file into the card creator (clips over 3s go to Sentence Audio). Checked against extension 1.30.15.
