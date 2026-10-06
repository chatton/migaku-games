#!/usr/bin/env bash
# One-step setup on Linux or macOS: start the frame server, check this machine, and print how to
# bind the hotkey. Safe to run again (e.g. after `git pull`): it updates what's already there.
#
#   ./install.sh               container if Docker/podman compose works, else native
#   ./install.sh --container   the published image with docker/podman compose (compose.yaml)
#   ./install.sh --native      a venv + user service, no containers (tools/native.sh)
#   ./install.sh --native --no-service    native, started by hand with `tools/native.sh run`
#
# Windows: see the README (AutoHotkey + Docker Desktop).
set -euo pipefail

ROOT="$(cd "$(dirname "$0")" && pwd)"
PORT="${MIGAKU_PORT:-8765}"
URL="http://localhost:$PORT"
IMAGE="ghcr.io/chatton/migaku-games"
mode="" service_flag=""

say() { printf '\n\033[1m==> %s\033[0m\n' "$*"; }
warn() { printf '\033[33mwarning:\033[0m %s\n' "$*" >&2; }
die() { printf '\033[31merror:\033[0m %s\n' "$*" >&2; exit 1; }
up() { python3 -c "import urllib.request,sys; urllib.request.urlopen(sys.argv[1] + '/api/config', timeout=2)" "$URL" 2>/dev/null; }
wait_up() { for _ in $(seq 1 "$1"); do up && return 0; sleep 1; done; return 1; }

for arg in "$@"; do
  case "$arg" in
    --container) mode=container ;;
    --native) mode=native ;;
    --no-service) service_flag=--no-service ;;
    -h|--help) sed -n '2,11p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) die "unknown option $arg (see ./install.sh --help)" ;;
  esac
done
[ -n "$service_flag" ] && [ "$mode" = container ] && die "--no-service only applies to --native"
[ -n "$service_flag" ] && mode=native

case "$(uname -s)" in Linux|Darwin) ;; *) die "this script is for Linux and macOS; on Windows follow the README" ;; esac
command -v python3 >/dev/null || die "python3 not found: the hotkey client needs Python 3.9+"
python3 -c 'import sys; sys.exit(sys.version_info < (3, 9))' || die "the hotkey client needs Python 3.9+ ($(python3 --version 2>&1))"

# docker compose or podman compose, whichever works (podman first where there's no docker).
compose=""
find_compose() {
  local engines="docker podman"
  command -v docker >/dev/null || engines="podman docker"
  for e in $engines; do
    if command -v "$e" >/dev/null && "$e" compose version >/dev/null 2>&1; then compose="$e compose"; return 0; fi
  done
  return 1
}

if [ -z "$mode" ]; then
  if find_compose; then mode=container; else mode=native; fi
  say "server: $mode (choose with --container or --native)"
fi

native_installed() { [ -f "${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user/migaku-games.service" ] || [ -f "$HOME/Library/LaunchAgents/com.migaku-games.server.plist" ]; }

if [ "$mode" = container ]; then
  if ! find_compose; then
    if command -v podman >/dev/null; then
      die "podman is here but 'podman compose' has no provider: install one (brew install podman-compose, or your package manager's podman-compose), or use --native"
    fi
    die "no docker/podman compose found: install one, or use --native"
  fi
  native_installed && die "a native server is installed; remove it first (tools/native.sh uninstall) or use --native"
  engine="${compose%% *}"
  say "starting the frame server with $compose"
  cd "$ROOT"
  mkdir -p data
  if $compose pull 2>/dev/null; then
    $compose up -d
  else
    warn "couldn't pull $IMAGE (private image: '$engine login ghcr.io' with a token that has read:packages); building it from this checkout instead (a few minutes)"
    $compose up -d --build
  fi
  if [ "$engine" = podman ] && command -v systemctl >/dev/null; then
    # Rootless podman restarts "restart: always" containers after a reboot only with this.
    systemctl --user enable --now podman-restart.service >/dev/null 2>&1 ||
      warn "couldn't enable podman-restart.service; the hotkey will start the server after a reboot instead (slowly)"
  fi
  wait_up 120 || die "the server didn't answer on $URL; see: $compose logs"
else
  say "installing the native frame server (tools/native.sh)"
  "$ROOT/tools/native.sh" install $service_flag
  if [ -n "$service_flag" ]; then
    say "start the server now in another terminal: $ROOT/tools/native.sh run"
  fi
fi

if up; then
  say "frame server is up: $URL"
fi

say "checking this machine (migaku_games.py --doctor)"
doctor="$(python3 "$ROOT/migaku_games.py" --doctor --server "$URL" 2>/dev/null || true)"
printf '%s\n' "$doctor"
if printf '%s\n' "$doctor" | grep -q "^  \* .*missing"; then
  warn "a tool for this desktop is missing (the * lines above that say 'missing'): install it before using the hotkey"
fi
if printf '%s\n' "$doctor" | grep -q "^browser: Brave not found"; then
  warn "Brave isn't installed: e.g. flatpak install flathub com.brave.Browser (or set host.browser in config/config.yaml)"
fi

# The hotkey: what to bind, and where, for the desktop this is running on.
hotkey="python3 $ROOT/migaku_games.py --overlay"
desktop="$(printf '%s %s' "${XDG_CURRENT_DESKTOP:-}" "${DESKTOP_SESSION:-}" | tr '[:upper:]' '[:lower:]')"
case "$(uname -s)/$desktop" in
  Darwin/*) where="Shortcuts.app (a shortcut with 'Run Shell Script'), or skhd/Hammerspoon; allow it to control Brave when asked" ;;
  */*kde*|*/*plasma*) where="System Settings → Keyboard → Shortcuts → Add New → Command or Script (e.g. Meta+J)" ;;
  */*gnome*|*/*ubuntu*) where="Settings → Keyboard → View and Customise Shortcuts → Custom Shortcuts" ;;
  */*sway*) where="your sway config: bindsym \$mod+j exec $hotkey" ;;
  */*hyprland*) where="your hyprland config: bind = SUPER, J, exec, $hotkey" ;;
  *) where="your desktop's keyboard shortcut settings" ;;
esac
[ "$PORT" != 8765 ] && hotkey="$hotkey --server $URL"

cat <<MSG

$(printf '\033[1m')Done. Next steps:$(printf '\033[0m')

  1. Brave: install the Migaku extension and log in.
  2. Open the live window once from a terminal, and authorise Migaku on that page:
       $hotkey
  3. Bind a hotkey to that same command:
       $where
  4. Add your game to config/config.yaml (a profile, then active_profile) and press
     "Reload config" on $URL/settings.html.

Gallery: $URL    Logs: python3 migaku_games.py --doctor
MSG
