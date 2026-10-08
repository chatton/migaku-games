# migaku-games

Look up Japanese in any game with [Migaku](https://migaku.com): press a hotkey, the screen is
captured and OCR'd, and a browser window over the game shows the same picture with hoverable
text. Migaku's extension does the rest: lookups, translation, and cards with the game frame.
Frames are transient: each becomes a Migaku card (studied in Migaku) or is discarded.

Works on Linux (KDE, GNOME, Sway, Hyprland, X11), macOS and Windows. Built and tested first for
Bazzite in **Desktop Mode** (KDE Plasma). Bazzite's Game Mode (gamescope) isn't supported: it has
no desktop shortcuts, screenshot tool or second window to show the overlay in.

| Part | Runs | What it does |
|---|---|---|
| **Frame server** (`server.py`) | Docker / podman, or natively as a user service; always on | OCR ([meikiocr](https://github.com/rtr46/meikiocr)), web pages, config, frames |
| **Brave + Migaku** | your desktop | the "Migaku Live" window and the gallery at http://localhost:8765 |
| **Hotkey client** (`migaku_games.py`, `migaku_host/`) | your desktop, plain Python 3.9+ (no installs) | started by your OS's shortcut on each press: capture, upload, show/hide the window |

There is no background key listener: you bind a key in your desktop's shortcut settings to run
the client, which does its job and exits.

## Setup

**Quick setup (Linux, macOS)**: clone, then run the install script. It starts the frame server
(in a container if Docker or podman compose works, otherwise natively), checks this machine with
`--doctor` and warns about anything missing, and prints the exact hotkey command and where your
desktop binds it. Run it again after `git pull` to update.

```sh
git clone https://github.com/chatton/migaku-games.git ~/migaku-games && cd ~/migaku-games
./install.sh               # or --container / --native; --native --no-service: no user service
```

Then do steps 3 to 5 below (Brave + Migaku, the hotkey, your game profile). The steps by hand:

1. **Start the server**, in a container (below) or [natively](#native-server-no-container):

   ```sh
   git clone https://github.com/chatton/migaku-games.git ~/migaku-games && cd ~/migaku-games
   docker login ghcr.io       # the image is private: your GitHub user + a token with read:packages
   docker compose pull
   docker compose up -d       # `--build` builds from this checkout instead of pulling
   ```

   **Bazzite / Fedora (podman)**: Bazzite ships podman, not Docker (except the `-dx` images).
   `podman compose` needs a compose provider: if `podman compose version` fails, install one
   (`brew install podman-compose`). Then use `podman login ghcr.io`, `podman compose pull` and
   `podman compose up -d`, and run `systemctl --user enable podman-restart` so the server comes
   back after a reboot (the hotkey can start it too, but the first start is slow).

   Open http://localhost:8765: the gallery should load.

2. **Check this machine**: `python3 ~/migaku-games/migaku_games.py --doctor` shows the desktop it
   detected, which screenshot and window tools it will use, what's missing, whether the server
   answers, and where the logs are. Install anything it lists as missing for your desktop (e.g.
   `spectacle` on KDE, PyGObject (`python3-gobject`) on GNOME Wayland for the screenshot portal (`gnome-screenshot` elsewhere), `grim` on Sway/Hyprland, `scrot` or `maim` on X11).

3. **Brave + Migaku**: install the Migaku extension and log in. Then run
   `python3 ~/migaku-games/migaku_games.py --overlay` once **from a terminal**: it opens the
   "Migaku Live" window. Authorise Migaku on that page and leave the window open.

4. **Bind the hotkey** to `python3 /home/YOU/migaku-games/migaku_games.py --overlay` (use the full
   path; `~` isn't expanded everywhere). Any free key; `Meta+J` is a good choice.

   | Desktop | Where |
   |---|---|
   | KDE Plasma | System Settings → Keyboard → Shortcuts → Add New → Command or Script |
   | GNOME | Settings → Keyboard → View and Customise Shortcuts → Custom Shortcuts |
   | Sway / Hyprland | `bindsym` / `bind` in the compositor config |
   | macOS | Shortcuts.app (Run Shell Script) or skhd/Hammerspoon; allow the terminal to control Brave when asked |
   | Windows | AutoHotkey: `#j::Run "pythonw C:\path\migaku-games\migaku_games.py --overlay"` |

5. **Your game profile**: in `config/config.yaml`, add a profile for the game and make it
   `active_profile` (see [Config](#config)).

**Game window mode**: start with the game's normal fullscreen; on Linux, Proton games usually
behave like borderless windows and the overlay can cover them. If the overlay flickers, the game
minimises, or captures come out black (common with exclusive fullscreen on Windows), switch the
game to borderless or windowed.

### Native server (no container)

The same server can run straight from the checkout instead: a Python venv in `.venv`, the OCR
models and JMdict table cached locally, and a user service that starts it at login (systemd on
Linux, launchd on macOS). Use it where containers are awkward, or on macOS for Apple's Vision
OCR. Run either the container or the native server, not both: they share port 8765.

```sh
git clone https://github.com/chatton/migaku-games.git ~/migaku-games && cd ~/migaku-games
tools/native.sh install       # venv + packages, models, JMdict (~5 min first time), then the service
tools/native.sh status        # venv, JMdict, service, and whether the server answers
```

- **Needs** Python 3.10+ with `venv` (Bazzite/Fedora and macOS with Homebrew Python have it;
  Debian/Ubuntu: `sudo apt install python3-venv`) and about 1 GB of disk. Another interpreter:
  `PYTHON=python3.13 tools/native.sh install`.
- **The service**: `~/.config/systemd/user/migaku-games.service` (Linux; `journalctl --user -u
  migaku-games`) or `~/Library/LaunchAgents/com.migaku-games.server.plist` (macOS). The hotkey
  client starts it if the server isn't answering, as it would the container.
- **Without a service**: `tools/native.sh install --no-service`, then `tools/native.sh run` in a
  terminal whenever you play.
- **OCR engine**: meiki on Linux; on macOS the native server defaults to Apple Vision (needs Xcode's
  command line tools for `swiftc`). Set `ocr_engine: meiki` in the config for meiki there.
- **Switching from the container**: `docker compose down` (or `podman compose down`) first. If
  rootful Docker wrote `data/`, it's owned by root: `sudo chown -R "$USER" data`.
- **Removing it**: `tools/native.sh uninstall` stops and removes the service; delete `.venv/` and
  `build/` to free the space.

## Playing

1. **Press the hotkey** on some dialogue. The screen is captured and shown fullscreen in the
   "Migaku Live" window, lined up with the game; the hoverable text follows when OCR finishes
   (about a second). If the profile has `freeze: true`, the game is paused meanwhile.
2. **Read and mine**:
   - hover words for Migaku's lookups (with its known/unknown underlines);
   - `y` translates the hovered box (Migaku's translator), `t` all boxes; `c` toggles
     colour-matched words between the caption and the Japanese;
   - `E` on a word sends it and its sentence to Migaku's card creator and copies the frame to your
     clipboard: press Ctrl+V (Cmd+V) in the card creator to put the frame on the card. Turn off
     image search in the card creator's settings to skip Migaku's stock image.
   - `?` lists every shortcut.
3. **Press the hotkey again**: the window hides and a frozen game resumes. (If you Alt+Tab back
   to the game instead, the live window is still "shown": the next press hides it, and the one
   after captures. Hiding it with the hotkey avoids that.)

Where a desktop doesn't let one app raise another's window (GNOME on Wayland), the client runs
in **follow mode**: the hotkey still captures, and the live window (kept on a second screen, or
one Alt+Tab away) shows the new frame; a notification says it's ready.

Other ways in: `python3 migaku_games.py` (drag-select a region, opens a tab), `--full`,
`--image x.png`, or drop/paste an image on the gallery page.

## Web UI

- **Frames** (`/`): recent captures, newest first, filterable by game; pin or delete.
- **Viewer**: the frame with hoverable text; translate, OCR text and transcript toggles, caption
  size and position, colour matching, older/newer frames.
- **Config** (`/settings.html`): the loaded config, warnings and errors, and **Reload config**.

## Config

Everything about how the app behaves is declared in `config/config.yaml`, mounted read-only into
the container (the shipped file documents every option). Edit it, then press **Reload config**
on the Config page; an invalid file is rejected with the reason and the previous config stays.

```yaml
retention_hours: 24          # unpinned frames older than this are deleted; 0 keeps them
copy_frame_on_card: true     # E copies the frame for pasting into the card creator
translation_colours: false   # colour-matched words start off; `c` toggles
active_profile: ff8
profiles:
  ff8:
    name: Final Fantasy VIII
    freeze: false            # experimental: pause the game while the overlay is up
    process: ""              # what to freeze; empty = the running Steam game (Linux)
host:                        # the hotkey client on this machine
  capture: auto              # or spectacle, portal, gnome-screenshot, grim, maim, scrot, import, screencapture, windows
  window: auto               # or kwin, sway, hyprland, xdotool, jxa, windows, follow
  browser: ""                # e.g. "flatpak run com.brave.Browser"; empty finds Brave
  notifications: true        # progress notifications; errors are always shown
keybindings:                 # viewer shortcuts; unlisted ones keep their defaults
  translate: y
```

**Game profiles**: one per game; switch `active_profile` when you switch games. Captures are
tagged with its name.

**Freeze the game (experimental)**: pauses the game's processes while the overlay is up (SIGSTOP
on Linux/macOS, NtSuspendProcess on Windows) and resumes them on hide. On Linux with no `process`
set, the running Steam game is frozen (found by Steam's `reaper SteamLaunch` process); elsewhere,
or for non-Steam games, set `process` to part of the executable name (`duckstation`, `FF8_EN`).
Not for online games. If a game is ever left frozen: `python3 migaku_games.py --resume`.

**Colour-matched translations** (`c`): words in the English caption and the Japanese words they
translate share a colour, underlined on the picture. Matched locally: Janome splits the Japanese
into words and [JMdict](https://www.edrdg.org/jmdict/j_jmdict.html) (EDRDG, CC BY-SA 4.0) gives
their meanings, so content words match and grammar doesn't.

## Updating

```sh
cd ~/migaku-games && git pull && ./install.sh                 # either kind of server
cd ~/migaku-games && git pull && docker compose pull && docker compose up -d   # or by hand: container
cd ~/migaku-games && git pull && tools/native.sh install      # or by hand: native (restarts it)
```

`web/` and `config/` are mounted from the checkout; the server code comes from the image, so pull
both together. Images are published for version tags (`v0.0.1`, ...); `latest` is the newest tag.

## Troubleshooting and logs

Everything is logged in detail, so a problem can be diagnosed from the logs alone.

- **Hotkey client** (every press: environment, desktop detection, chosen tools, each command with
  its exit code, output and timing, every server call, full tracebacks):
  - Linux: `~/.local/state/migaku-games/host.log`
  - macOS: `~/Library/Logs/migaku-games/host.log`
  - Windows: `%LOCALAPPDATA%\migaku-games\host.log`
- **Server** (requests with timings, OCR per frame with timing and text, config loads, and errors
  reported by the viewer pages): `data/logs/server.log` in the checkout, or `docker compose logs -f`.
  More detail (every poll): `MIGAKU_LOG_LEVEL: DEBUG` under `environment:` in `compose.yaml`.
- **KDE**: the KWin script's own output: `journalctl --user -b | grep migaku-games`.
- `python3 migaku_games.py --doctor` summarises the setup; add `-v` to any command to see the
  detailed log in the terminal.

Both logs carry timestamps with their UTC offset, so they line up.

| Symptom | Look at |
|---|---|
| Pressing the hotkey does nothing | A desktop notification should say why; if not, the shortcut isn't running the command: check the path in the shortcut and the host log |
| "frame server not reachable" | `docker compose ps` / `docker compose logs` (native: `tools/native.sh status`); the client starts the server itself, but the first start takes a while |
| The live window never comes to the front | `--doctor` for the window backend; on KDE, the journal line above shows how many windows the script matched (it matches Chromium browsers' windows titled "Migaku Live") |
| A page says 403 / "only answers to localhost" | the server refuses other host names; open it as http://localhost:8765, or add the name to `MIGAKU_ALLOWED_HOSTS` under `environment:` in `compose.yaml` |
| Hovering does nothing | the server log says on each page load whether Migaku is active; authorise Migaku for `localhost:8765` |
| Wrong or noisy text | the server log's OCR line shows exactly what was read |

## How it works

- **Overlay window**: `viewer.html?live` waits on `/api/latest` for new frames (a pending request
  isn't throttled while the window is minimised) and reports focus/visibility to `/api/live`. The
  client marks it shown after raising it; pressed again while shown, it hides it. Minimising the
  window any other way clears the flag.
- **Fast first paint**: the client uploads with `wait=0` and the server replies once the picture is
  saved; OCR runs in the background and `/api/frames/<id>/ocr` waits for it.
- **Text layout**: each OCR line is a relatively positioned inline span sized to its box, so
  Migaku reads a dialogue box as whole sentences and its highlights land on the picture's text.
- **The picture is its own page** (`picture.html` in an iframe): Migaku only takes an image for a
  card from a hovered `<img>` on a page with no text.
- **Platform layer** (`migaku_host/`): one backend per desktop for capture (`capture.py`), the window
  (`window.py`), notifications, freezing and the browser, picked from the detected desktop
  (`desktop.py`) or the config's `host` section.
- **OCR engines**: `meiki` (models baked into the image, or cached by `tools/native.sh`) and
  `vision` (Apple Vision, `ocr/vision_ocr.swift`; macOS only, with the [native
  server](#native-server-no-container)).

## Development

```sh
docker compose up -d --build
python3 -m unittest tests.test_host               # the client's platform layer (stdlib only)
python3 -m unittest tests.test_config             # config validation (needs PyYAML, e.g. .venv)
docker run --rm -v "$PWD:/src" -w /src ghcr.io/chatton/migaku-games:latest \
  python -m unittest tests.test_config tests.test_align tests.test_host
# End-to-end server checks against a throwaway container and config folder (the test rewrites it):
mkdir -p /tmp/smoke-config && cp config/config.yaml /tmp/smoke-config/
docker run --rm -d --name migaku-smoke -p 8799:8765 -v /tmp/smoke-config:/config ghcr.io/chatton/migaku-games:latest
python3 tests/smoke_test.py http://localhost:8799 /tmp/smoke-config && docker stop migaku-smoke
```

`viewer.html?debug` also posts the overlay/Migaku DOM to `data/debug/dom.html`.

CI (`.github/workflows/docker.yml`) runs on every push: the image is built and its unit and smoke
tests run, and the client's checks run on Fedora, Ubuntu, Debian, Arch and Alpine, bare and with
desktop tools (`tests/check_host_distro.sh`). Pushing a version tag also publishes
`ghcr.io/chatton/migaku-games` for linux/amd64 and linux/arm64:

```sh
git tag v0.0.1 && git push origin v0.0.1
```

`samples/` holds screenshots of commercial games for OCR testing (Famitsu press shots of FF7/8/9
and Persona 5 Royal; `ff8r_*` are 1080p Steam store shots of FF8 Remastered in Japanese); keep the
repo private.

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
