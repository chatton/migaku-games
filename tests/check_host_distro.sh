#!/bin/sh
# Host client checks on one Linux distribution, run by CI inside a plain distro image:
#   tests/check_host_distro.sh none      # bare: Python only
#   tests/check_host_distro.sh desktop   # with grim, scrot, xdotool and notify-send installed
# Runs the unit tests, checks backend choice against the real tools on PATH, and runs
# --doctor (no frame server here) to check it reports and logs without crashing.
set -eu
tools="$1"
cd "$(dirname "$0")/.."
python3 --version
python3 -m unittest -v tests.test_host

python3 - "$tools" <<'PY'
import sys
from migaku_host import capture, desktop, window
from migaku_host.util import AppError
tools = sys.argv[1]
sway = desktop.detect({"XDG_CURRENT_DESKTOP": "sway", "WAYLAND_DISPLAY": "wayland-1"}, system="linux")
x11 = desktop.detect({"XDG_CURRENT_DESKTOP": "XFCE", "DISPLAY": ":0"}, system="linux")
kde = desktop.detect({"XDG_CURRENT_DESKTOP": "KDE", "XDG_SESSION_TYPE": "wayland"}, system="linux")
if tools == "desktop":
    assert capture.choose(sway).name == "grim", capture.choose(sway).name
    assert capture.choose(x11).name == "scrot", capture.choose(x11).name
    assert window.choose(x11).name == "xdotool", window.choose(x11).name
    assert capture.choose(kde).name == "grim"  # no spectacle: falls back to an installed tool
else:
    try:
        capture.choose(kde)
        raise SystemExit("expected no capture tool")
    except AppError as e:
        assert "spectacle" in str(e), e
    # KWin control only needs a D-Bus tool, which some base images (Fedora: gdbus) already have.
    expected = "kwin" if window.KWin.dbus_tool() else "follow"
    assert window.choose(kde).name == expected, (window.choose(kde).name, expected)
print("backend choice ok for", tools)
PY

python3 migaku_games.py --doctor --server http://127.0.0.1:9 | tee /tmp/doctor.txt
grep -q "NOT reachable" /tmp/doctor.txt
log="${XDG_STATE_HOME:-$HOME/.local/state}/migaku-games/host.log"
grep -q "doctor:" "$log" && echo "host log written: $log"
