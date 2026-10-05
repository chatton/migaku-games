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
    "caption_larger": ("+", "larger captions"),
    "caption_smaller": ("-", "smaller captions"),
    "previous_frame": ("[", "older frame"),
    "next_frame": ("]", "newer frame"),
    "help": ("?", "this shortcut sheet"),
}
# Keys the Migaku extension uses on the page; binding them would fight with it.
MIGAKU_KEYS = {"e", "q", "1", "2", "3", "4", "u", "k", "i"}

ENGINES = ("vision", "meiki")
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
    _only(raw, ("ocr_engine", "retention_hours", "active_profile", "profiles", "keybindings"), "config")

    engine = raw.get("ocr_engine")
    if engine is not None and engine not in ENGINES:
        raise ConfigError(f"ocr_engine: must be one of {', '.join(ENGINES)}, got {engine!r}")

    hours = raw.get("retention_hours", 24)
    if isinstance(hours, bool) or not isinstance(hours, (int, float)) or hours < 0:
        raise ConfigError(f"retention_hours: expected a number >= 0 (0 keeps frames forever), got {hours!r}")

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
        "active_profile": active,
        "profiles": profiles,
        "keybindings": keys,
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
