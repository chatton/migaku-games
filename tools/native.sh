#!/usr/bin/env bash
# Run the frame server natively (no Docker/podman): a Python venv in .venv, the OCR models and the
# JMdict table cached locally, and a user service that starts it at login.
#
#   tools/native.sh install              venv + models + JMdict, then install and start the service
#   tools/native.sh install --no-service the same without the service (start it with `run`)
#   tools/native.sh run                  the server in the foreground (Ctrl+C stops it)
#   tools/native.sh status               is the service installed / running / answering?
#   tools/native.sh uninstall            stop and remove the service (keeps .venv, build/, data/)
#
# Linux: a systemd user unit (~/.config/systemd/user/migaku-games.service).
# macOS: a launchd agent (~/Library/LaunchAgents/com.migaku-games.server.plist).
# PYTHON picks the interpreter for the venv (default python3; 3.10 or newer).
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
case "$ROOT" in *[[:space:]]*) echo "error: move the checkout to a path without spaces ($ROOT)" >&2; exit 1;; esac
VENV="$ROOT/.venv"
PY="$VENV/bin/python"
JMDICT="$ROOT/build/jmdict.sqlite"
PORT="${MIGAKU_PORT:-8765}"
URL="http://localhost:$PORT"
UNIT_NAME="migaku-games.service"
UNIT="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user/$UNIT_NAME"
LABEL="com.migaku-games.server"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"

say() { printf '\033[1m==>\033[0m %s\n' "$*"; }
die() { printf 'error: %s\n' "$*" >&2; exit 1; }
os() { case "$(uname -s)" in Linux) echo linux;; Darwin) echo mac;; *) echo other;; esac; }
up() { "$([ -x "$PY" ] && echo "$PY" || echo "${PYTHON:-python3}")" -c "import urllib.request,sys; urllib.request.urlopen(sys.argv[1] + '/api/config', timeout=2)" "$URL" 2>/dev/null; }

setup_venv() {
  local python="${PYTHON:-python3}"
  command -v "$python" >/dev/null || die "$python not found (set PYTHON=/path/to/python3)"
  "$python" -c 'import sys; sys.exit(sys.version_info < (3, 10))' ||
    die "$("$python" --version 2>&1) is too old; the server needs Python 3.10+ (set PYTHON=...)"
  if [ ! -x "$PY" ]; then
    say "creating $VENV with $("$python" --version 2>&1)"
    "$python" -m venv "$VENV" || die "couldn't create the venv (Debian/Ubuntu: install python3-venv)"
  fi
  say "installing the server's Python packages (requirements-meiki.txt)"
  "$PY" -m pip install -q --upgrade pip
  "$PY" -m pip install -q -r "$ROOT/requirements-meiki.txt"
  say "downloading the meikiocr models (cached in ~/.cache/huggingface)"
  "$PY" -c "from meikiocr import MeikiOCR; MeikiOCR()"
  if [ ! -s "$JMDICT" ]; then
    say "building the JMdict table for colour-matched translations"
    mkdir -p "$ROOT/build"
    "$PY" "$ROOT/tools/build_jmdict.py" "$JMDICT"
  fi
  mkdir -p "$ROOT/data"
}

# The models are cached by now: don't ask Hugging Face for updates on every OCR.
server_env() { echo "HF_HUB_OFFLINE=1 MIGAKU_JMDICT=$JMDICT MIGAKU_PORT=$PORT PYTHONUNBUFFERED=1"; }

install_systemd() {
  mkdir -p "$(dirname "$UNIT")"
  local env_lines="" kv
  for kv in $(server_env); do env_lines="${env_lines}Environment=$kv
"; done
  cat > "$UNIT" <<EOF
# Installed by tools/native.sh; remove with: tools/native.sh uninstall
[Unit]
Description=migaku-games frame server (OCR, web UI)
After=network.target

[Service]
WorkingDirectory=$ROOT
${env_lines}ExecStart=$PY $ROOT/server.py
Restart=on-failure
RestartSec=3

[Install]
WantedBy=default.target
EOF
  systemctl --user daemon-reload
  systemctl --user enable --now "$UNIT_NAME"
  systemctl --user restart "$UNIT_NAME"  # picks up a changed checkout on re-install
}

install_launchd() {
  mkdir -p "$(dirname "$PLIST")"
  local env_xml="" kv
  for kv in $(server_env); do
    env_xml="$env_xml<key>${kv%%=*}</key><string>${kv#*=}</string>"
  done
  cat > "$PLIST" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<!-- Installed by tools/native.sh; remove with: tools/native.sh uninstall -->
<plist version="1.0"><dict>
  <key>Label</key><string>$LABEL</string>
  <key>ProgramArguments</key><array><string>$PY</string><string>$ROOT/server.py</string></array>
  <key>WorkingDirectory</key><string>$ROOT</string>
  <key>EnvironmentVariables</key><dict>$env_xml</dict>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><dict><key>SuccessfulExit</key><false/></dict>
  <key>StandardOutPath</key><string>$ROOT/data/logs/launchd.log</string>
  <key>StandardErrorPath</key><string>$ROOT/data/logs/launchd.log</string>
</dict></plist>
EOF
  mkdir -p "$ROOT/data/logs"
  launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
  launchctl bootstrap "gui/$(id -u)" "$PLIST"
}

wait_up() {
  for _ in $(seq 1 60); do up && return 0; sleep 1; done
  return 1
}

cmd_install() {
  local service=1
  [ "${1:-}" = "--no-service" ] && service=0
  if up && ! service_running; then
    die "something already answers on $URL (the container? stop it: docker compose down / podman compose down)"
  fi
  if [ "$service" = 1 ]; then  # before the slow part: can this machine run the service at all?
    case "$(os)" in
      linux) systemctl --user show-environment >/dev/null 2>&1 ||
        die "no systemd user session here; use 'install --no-service' and start it with 'run'" ;;
      mac) ;;
      *) die "no service support on $(uname -s); use 'install --no-service' and 'run'" ;;
    esac
  fi
  setup_venv
  if [ "$service" = 0 ]; then
    say "installed; start the server with: tools/native.sh run"
    return
  fi
  case "$(os)" in
    linux) say "installing the systemd user service ($UNIT)"; install_systemd ;;
    mac) say "installing the launchd agent ($PLIST)"; install_launchd ;;
  esac
  wait_up || die "the server didn't answer on $URL; see data/logs/server.log ($(os) service logs: $(logs_hint))"
  say "running: $URL (starts at login; logs: data/logs/server.log)"
}

service_running() {
  case "$(os)" in
    linux) systemctl --user is-active --quiet "$UNIT_NAME" 2>/dev/null ;;
    mac) launchctl print "gui/$(id -u)/$LABEL" >/dev/null 2>&1 ;;
    *) return 1 ;;
  esac
}

logs_hint() {
  case "$(os)" in
    linux) echo "journalctl --user -u $UNIT_NAME" ;;
    mac) echo "data/logs/launchd.log" ;;
    *) echo "the terminal" ;;
  esac
}

cmd_run() {
  [ -x "$PY" ] || die "not installed yet: tools/native.sh install --no-service"
  service_running && die "the service is already running it ($URL); tools/native.sh uninstall to stop it"
  cd "$ROOT"
  # shellcheck disable=SC2046
  exec env $(server_env) "$PY" "$ROOT/server.py" "$@"
}

cmd_status() {
  local installed=no
  [ -f "$UNIT" ] || [ -f "$PLIST" ] && installed=yes
  echo "venv:      $([ -x "$PY" ] && "$PY" --version || echo 'not created')"
  echo "jmdict:    $([ -s "$JMDICT" ] && echo "$JMDICT" || echo 'not built')"
  echo "service:   installed=$installed running=$(service_running && echo yes || echo no)"
  echo "server:    $URL $(up && echo up || echo 'not answering')"
}

cmd_uninstall() {
  case "$(os)" in
    linux)
      if [ -f "$UNIT" ]; then
        systemctl --user disable --now "$UNIT_NAME" || true
        rm -f "$UNIT"
        systemctl --user daemon-reload
      fi ;;
    mac)
      launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
      rm -f "$PLIST" ;;
  esac
  say "service removed; .venv/, build/ (JMdict) and data/ (frames) are kept: delete them to free the space"
}

case "${1:-}" in
  install) shift; cmd_install "$@" ;;
  run) shift; cmd_run "$@" ;;
  status) cmd_status ;;
  uninstall) cmd_uninstall ;;
  *) sed -n '2,13p' "$0" | sed 's/^# \{0,1\}//'; exit 2 ;;
esac
