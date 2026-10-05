# migaku-games

Look up Japanese in any game with [Migaku](https://migaku.com): press a hotkey, the screen is
captured and OCR'd, and a Brave window over the game shows the same picture with hoverable text.
Migaku's extension does the rest: lookups, translation, and cards with the game frame on them.

Built for Bazzite (KDE Plasma, desktop mode); also runs on macOS for development.

- **Frame server** (`server.py`, in Docker): OCR ([meikiocr](https://github.com/rtr46/meikiocr),
  trained on game text), frame storage, and the web UI at http://localhost:8765.
- **Capture client** (`migaku_games.py`, on the host, stdlib Python): screenshot, upload, and
  showing/hiding the overlay window.

## Setup

```sh
gh repo clone chatton/migaku-games ~/migaku-games && cd ~/migaku-games
docker login ghcr.io                # the image is private: GitHub user + a token with read:packages
docker compose up -d                # or podman compose; `--build` builds from this checkout instead
```

Then:

1. In Brave, install Migaku and log in.
2. Bind `python3 ~/migaku-games/migaku_games.py --overlay` to a global shortcut (KDE: System
   Settings → Shortcuts → Add New → Command or Script, e.g. `Meta+J`).
3. Edit `config/config.yaml`: add a profile for the game you're playing and make it `active_profile`
   (see [Config](#config)).
4. Run the game borderless windowed (exclusive fullscreen games minimise when covered).

With rootless podman, `systemctl --user enable podman-restart` brings the container back after a
reboot.

## Playing

1. **Press the hotkey** on some dialogue. The screen under the mouse is shown fullscreen in the
   "Migaku Live" Brave window, lined up with the game; the hoverable text follows when OCR
   finishes (about a second). The first press opens that window: authorise Migaku on it once and
   leave it open. It swaps frames in place, so Migaku stays active.
2. **Read and mine:** hover words; `y` translates the hovered box (through Migaku's translator);
   `E` on a word sends it and its sentence to the card creator, then `E` on the picture (away from
   text) adds the frame. Turn off image search in the card creator's settings to skip Migaku's
   stock image. `?` lists every shortcut.
3. **Press the hotkey again** to hide the window and go back to the game.

Works with anything on screen: Steam, GOG, emulators. Other ways in:

```sh
python3 migaku_games.py                  # drag-select a region, open it in a new tab
python3 migaku_games.py --image x.png    # an existing image
```

or drop/paste an image on the gallery page.

## Web UI

- **Frames** (`/`): every capture, newest first, filterable by game. Pin to keep, or delete.
- **Viewer**: the frame with hoverable text. Translate all boxes, show the OCR text or a
  transcript, resize and drag translation captions, step through older/newer frames.
- **Config** (`/settings.html`): the loaded config, warnings and errors, and the **Reload config** button.

`web/` is mounted into the container, so UI edits show up on reload; Python changes need
`docker compose up -d --build`.

## Config

Everything about how the app behaves is declared in one file, `config/config.yaml` (mounted
read-only at `/config`; the shipped file documents every option):

```yaml
ocr_engine: meiki          # optional; default: the server's
retention_hours: 24        # unpinned frames older than this are deleted; 0 keeps them
active_profile: ff8
profiles:
  ff8:
    name: Final Fantasy VIII
    freeze: false          # experimental, see below
    process: ""
keybindings:               # viewer shortcuts; unlisted ones keep their defaults
  translate: y
```

Edit it, then press **Reload config** on the Config page. An invalid file (unknown keys, a missing
active profile, two actions on one key, ...) is rejected with the reason shown there, and the
previous config stays in force. Keybindings that clash with Migaku's own keys get a warning.
Open viewers pick up new keybindings when they next get focus. Frames and pins are runtime state
in `./data`, not config.

**Game profiles:** one per game; switch `active_profile` when you switch games. Overlay captures
are tagged with the active profile's name (the gallery filters by it).

**Freeze the game (experimental, Linux):** while the overlay is up, the game's processes are paused
(SIGSTOP) and resumed (SIGCONT) when you hide it, so nothing moves on while you read. With no
process name set, the running Steam game is frozen (found by Steam's `reaper SteamLaunch`
process); otherwise the process whose executable name contains the given text (`duckstation`,
`retroarch`, ...). Not for online games. If a game is ever left frozen:
`python3 migaku_games.py --resume`.

## How it works

- **Overlay window:** `viewer.html?live` waits on `/api/latest` for new frames (a pending request
  isn't throttled while the window is minimised) and reports its focus to `/api/live`, which is
  how the hotkey decides between hiding the window and capturing. The window is raised and hidden
  by a one-off KWin script over `dbus-send` on KDE, or JXA on macOS (asks once to let the terminal
  control Brave).
- **Fast first paint:** uploads with `wait=0` return once the picture is saved; OCR runs in the
  background and `/api/frames/<id>/ocr` waits for it.
- **Text layout:** each OCR line is a relatively positioned inline span sized to its box, so
  Migaku reads a dialogue box as whole sentences and its highlights land on the picture's text.
- **The picture is its own page** (`picture.html` in an iframe): Migaku only sends an image to the
  card creator from a page with no text in it.
- **OCR engines:** `meiki` (the container's; models baked into the image) and `vision` (Apple
  Vision, `ocr/vision_ocr.swift`; macOS only, needs the server run natively with
  `python3 server.py --ocr vision`). Both print `{width, height, lines: [{text, conf, x, y, w, h}]}`.

## Development

```sh
docker compose up -d --build
python3 -m unittest tests.test_config   # config validation (needs PyYAML, e.g. .venv)
# End-to-end checks, against a throwaway container and config folder (the test rewrites it):
mkdir -p /tmp/smoke-config && cp config/config.yaml /tmp/smoke-config/
docker run --rm -d --name migaku-smoke -p 8799:8765 -v /tmp/smoke-config:/config ghcr.io/chatton/migaku-games:latest
python3 tests/smoke_test.py http://localhost:8799 /tmp/smoke-config && docker stop migaku-smoke
python3 make_test_image.py         # regenerate the synthetic samples (macOS font)
```

`viewer.html?debug` posts script errors and the overlay/Migaku DOM to `data/debug/dom.html`.

CI (`.github/workflows/docker.yml`) builds the image and runs the config and smoke tests on every
push. Pushing a version tag also publishes `ghcr.io/chatton/migaku-games` for
linux/amd64 and linux/arm64 (`latest`, `1.2.3`, `1.2`):

```sh
git tag v0.1.0 && git push origin v0.1.0
```

`samples/` holds screenshots of commercial games for OCR testing (Famitsu press shots of FF7/8/9 and
Persona 5 Royal; `ff8r_*` are 1080p Steam store shots of FF8 Remastered in Japanese); keep the repo private.

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
