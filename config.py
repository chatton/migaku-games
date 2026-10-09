"""The app's declarative config: one YAML file (config/config.yaml, mounted read-only into the
container), loaded at start and again on demand from the web UI's Reload button.

load_config(path) returns (config, warnings) with every default filled in, or raises
ConfigError naming what's wrong. A missing file means all defaults.
"""
import re
from pathlib import Path

import yaml

# Viewer actions that can be bound to a key, with their defaults and descriptions (shown in the
# viewer's shortcut sheet and the config page). Values are KeyboardEvent.key values.
ACTIONS = {
    "translate": ("y", "translate the hovered dialogue box"),
    "translate_all": ("t", "translate every dialogue box"),
    "toggle_ocr_text": ("v", "show the OCR text over the picture"),
    "toggle_transcript": ("h", "transcript"),
    "toggle_colours": ("c", "colour-matched translation words on/off"),
    "caption_larger": ("+", "larger captions"),
    "caption_smaller": ("-", "smaller captions"),
    "previous_frame": ("[", "older frame"),
    "next_frame": ("]", "newer frame"),
    "help": ("?", "this shortcut sheet"),
    "back_to_game": ("g", "live window: hide it and resume the game"),
    "toggle_fullscreen": ("f", "fullscreen on/off"),
}
# Keys the Migaku extension uses on the page; binding them would fight with it.
MIGAKU_KEYS = {"e", "q", "1", "2", "3", "4", "u", "k", "i"}

ENGINES = ("vision", "meiki")
# Host-side backends (migaku_host/capture.py and window.py; tests/test_host.py keeps these in sync).
CAPTURE_BACKENDS = ("auto", "screencapture", "windows", "spectacle", "portal", "gnome-screenshot", "grim", "maim", "scrot", "import")
WINDOW_BACKENDS = ("auto", "jxa", "windows", "kwin", "gnome", "sway", "hyprland", "xdotool", "follow")
PROFILE_ID = re.compile(r"^[\w-]{1,40}$")


class ConfigError(ValueError):
    pass


def _expect(value, kind, where):
    if not isinstance(value, kind):
        names = " or ".join(k.__name__ for k in (kind if isinstance(kind, tuple) else (kind,)))
        raise ConfigError(f"{where}: expected {names}, got {type(value).__name__} ({value!r})")
    return value


def _only(section: dict, allowed, where):
    unknown = set(section) - set(allowed)
    if unknown:
        raise ConfigError(f"{where}: unknown key(s) {', '.join(sorted(map(str, unknown)))}; "
                          f"allowed: {', '.join(allowed)}")


def parse(raw) -> tuple:
    """Validate the parsed YAML document and fill in defaults."""
    warnings = []
    raw = {} if raw is None else _expect(raw, dict, "config")
    _only(raw, ("ocr_engine", "retention_hours", "copy_frame_on_card", "translation_colours", "active_profile",
                "profiles", "keybindings", "host"), "config")

    host = _expect(raw.get("host") or {}, dict, "host")
    _only(host, ("capture", "window", "browser", "notifications", "overlay_profile", "clipboard"), "host")
    host = {
        "capture": str(host.get("capture", "auto")),
        "window": str(host.get("window", "auto")),
        "browser": str(_expect(host.get("browser") or "", str, "host.browser")).strip(),
        "notifications": _expect(host.get("notifications", True), bool, "host.notifications"),
        "overlay_profile": _expect(host.get("overlay_profile", True), (bool, str), "host.overlay_profile"),
        "clipboard": _expect(host.get("clipboard", True), bool, "host.clipboard"),
    }
    if host["capture"] not in CAPTURE_BACKENDS:
        raise ConfigError(f"host.capture: one of {', '.join(CAPTURE_BACKENDS)}, got {host['capture']!r}")
    if host["window"] not in WINDOW_BACKENDS:
        raise ConfigError(f"host.window: one of {', '.join(WINDOW_BACKENDS)}, got {host['window']!r}")

    engine = raw.get("ocr_engine")
    if engine is not None and engine not in ENGINES:
        raise ConfigError(f"ocr_engine: must be one of {', '.join(ENGINES)}, got {engine!r}")

    hours = raw.get("retention_hours", 24)
    if isinstance(hours, bool) or not isinstance(hours, (int, float)) or hours < 0:
        raise ConfigError(f"retention_hours: expected a number >= 0 (0 keeps frames forever), got {hours!r}")

    copy_frame = _expect(raw.get("copy_frame_on_card", True), bool, "copy_frame_on_card")
    colours = _expect(raw.get("translation_colours", False), bool, "translation_colours")

    profiles = {}
    for pid, prof in (_expect(raw.get("profiles") or {}, dict, "profiles")).items():
        where = f"profiles.{pid}"
        if not PROFILE_ID.match(str(pid)):
            raise ConfigError(f"{where}: ids are letters, digits, _ and - (up to 40)")
        prof = {} if prof is None else _expect(prof, dict, where)
        _only(prof, ("name", "freeze", "process"), where)
        profiles[str(pid)] = {
            "name": str(_expect(prof.get("name", pid), (str, int), f"{where}.name")).strip() or str(pid),
            "freeze": _expect(prof.get("freeze", False), bool, f"{where}.freeze"),
            "process": _expect(prof.get("process") or "", str, f"{where}.process").strip(),
        }

    active = raw.get("active_profile")
    if active is not None:
        active = str(active)
        if active not in profiles:
            raise ConfigError(f"active_profile: {active!r} isn't one of the profiles "
                              f"({', '.join(profiles) or 'none defined'})")

    keys = {name: default for name, (default, _) in ACTIONS.items()}
    bindings = _expect(raw.get("keybindings") or {}, dict, "keybindings")
    _only(bindings, tuple(ACTIONS), "keybindings")
    for name, key in bindings.items():
        key = str(_expect(key, (str, int), f"keybindings.{name}"))
        if not key:
            raise ConfigError(f"keybindings.{name}: empty key")
        keys[name] = key
    seen = {}
    for name, key in keys.items():
        if key.lower() in seen:
            raise ConfigError(f"keybindings: {key!r} is bound to both {seen[key.lower()]} and {name}")
        seen[key.lower()] = name
        if key.lower() in MIGAKU_KEYS:
            warnings.append(f"keybindings.{name}: {key!r} is also a Migaku shortcut on the page")

    return {
        "ocr_engine": engine,
        "retention_hours": float(hours),
        "copy_frame_on_card": copy_frame,
        "translation_colours": colours,
        "active_profile": active,
        "profiles": profiles,
        "keybindings": keys,
        "host": host,
    }, warnings


def load_config(path: Path) -> tuple:
    try:
        text = path.read_text()
    except FileNotFoundError:
        return parse(None)[0], [f"{path} not found; using defaults"]
    except OSError as e:
        raise ConfigError(f"can't read {path}: {e}") from None
    try:
        raw = yaml.safe_load(text)
    except yaml.YAMLError as e:
        raise ConfigError(f"{path.name} isn't valid YAML: {e}") from None
    return parse(raw)
