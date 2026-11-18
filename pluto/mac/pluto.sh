#!/bin/bash
# Run Pluto on a Mac in the background: starts at login, restarts if it crashes.
#
#   ./mac/pluto.sh install           set up and start (add --awake to stop the Mac idle-sleeping)
#   ./mac/pluto.sh status            is it running?
#   ./mac/pluto.sh logs              follow the log (Ctrl-C to stop watching)
#   ./mac/pluto.sh restart           after changing .env or updating the code
#   ./mac/pluto.sh stop | start
#   ./mac/pluto.sh uninstall         stop and remove the background job (keeps your data)
#
# Works with the bash that ships with macOS (3.2).
set -eu

LABEL="com.heeralal.pluto"
HERE="$(cd "$(dirname "$0")/.." && pwd)"            # the pluto/ folder
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
LOG_DIR="$HOME/Library/Logs/Pluto"
LOG="$LOG_DIR/pluto.log"
VENV_PY="$HERE/.venv/bin/python"
DOMAIN="gui/$(id -u)"

say()  { printf '%s\n' "$*"; }
fail() { printf 'Error: %s\n' "$*" >&2; exit 1; }

[ "$(uname -s)" = "Darwin" ] || fail "this script is for macOS. On other systems run: python bot.py"

find_python() {
  for c in python3.13 python3.12 python3.11 python3; do
    if command -v "$c" >/dev/null 2>&1 &&
       "$c" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)' 2>/dev/null; then
      command -v "$c"; return 0
    fi
  done
  return 1
}

is_loaded() { launchctl print "$DOMAIN/$LABEL" >/dev/null 2>&1; }

trim_log() {  # keep the log from growing forever
  if [ -f "$LOG" ] && [ "$(wc -c < "$LOG")" -gt 5000000 ]; then
    tail -n 2000 "$LOG" > "$LOG.tmp" && mv "$LOG.tmp" "$LOG"
  fi
}

do_stop() {
  if is_loaded; then launchctl bootout "$DOMAIN/$LABEL" 2>/dev/null || true; fi
}

do_start() {
  [ -f "$PLIST" ] || fail "not installed yet. Run: ./mac/pluto.sh install"
  mkdir -p "$LOG_DIR"; trim_log
  do_stop
  launchctl bootstrap "$DOMAIN" "$PLIST"
}

cmd_install() {
  awake=""
  [ "${1:-}" = "--awake" ] && awake="--awake"

  case "$HERE" in
    "$HOME/Documents"/*|"$HOME/Desktop"/*|"$HOME/Downloads"/*)
      say "Warning: macOS restricts background apps in Documents, Desktop and Downloads."
      say "         If Pluto can't start, move the Heera-AI folder to $HOME and install again." ;;
  esac

  if [ ! -x "$VENV_PY" ]; then
    py="$(find_python)" || fail "Python 3.11 or newer is needed. Install it with: brew install python@3.12"
    say "Creating the Python environment with $py ..."
    "$py" -m venv "$HERE/.venv"
  fi
  say "Installing requirements ..."
  "$VENV_PY" -m pip install --quiet --upgrade pip
  "$VENV_PY" -m pip install --quiet -r "$HERE/requirements.txt"

  if [ ! -f "$HERE/.env" ]; then
    cp "$HERE/.env.example" "$HERE/.env"
    chmod 600 "$HERE/.env"
    say ""
    say "I created $HERE/.env from the template."
    say "Fill in your keys (open -e \"$HERE/.env\"), then run this again: ./mac/pluto.sh install $awake"
    exit 1
  fi
  chmod 600 "$HERE/.env"

  say "Checking your settings ..."
  (cd "$HERE" && "$VENV_PY" bot.py --check)
  (cd "$HERE" && "$VENV_PY" -c 'import bot; bot.check_config()') ||
    fail "fix the settings above in .env, then run install again."

  mkdir -p "$HOME/Library/LaunchAgents" "$LOG_DIR"
  "$VENV_PY" "$HERE/mac/make_plist.py" --python "$VENV_PY" --workdir "$HERE" --log "$LOG" --out "$PLIST" $awake
  do_start
  sleep 3
  say ""
  cmd_status
  say ""
  say "Pluto now starts when you log in and restarts itself if it stops."
  if [ -n "$awake" ]; then
    say "Keep-awake is on: the Mac won't idle-sleep while Pluto runs (closing the lid still sleeps it)."
  else
    say "Reminders pause while the Mac sleeps. To stop idle sleep: ./mac/pluto.sh install --awake"
  fi
  say "Log: ./mac/pluto.sh logs"
}

cmd_status() {
  if ! [ -f "$PLIST" ]; then say "Pluto is not installed as a background app."; return 0; fi
  if is_loaded; then
    pid="$(launchctl print "$DOMAIN/$LABEL" 2>/dev/null | awk '/^[[:space:]]*pid = /{print $3; exit}')"
    if [ -n "$pid" ]; then say "Pluto is running (process $pid)."
    else say "Pluto is installed but not running right now (it may be restarting). See: ./mac/pluto.sh logs"; fi
  else
    say "Pluto is installed but stopped. Start it with: ./mac/pluto.sh start"
  fi
  if [ -f "$LOG" ]; then say "Last log line: $(tail -n 1 "$LOG")"; fi
}

cmd_uninstall() {
  do_stop
  rm -f "$PLIST"
  say "Removed the background job. Your data ($HERE/pluto.db), .env and logs are untouched."
}

case "${1:-}" in
  install)   shift; cmd_install "${1:-}" ;;
  uninstall) cmd_uninstall ;;
  start)     do_start; sleep 2; cmd_status ;;
  stop)      do_stop; say "Pluto stopped. It will start again at next login unless you uninstall." ;;
  restart)   do_start; sleep 2; cmd_status ;;
  status)    cmd_status ;;
  logs)      mkdir -p "$LOG_DIR"; touch "$LOG"; tail -n 50 -f "$LOG" ;;
  *)         sed -n '2,9p' "$0" | sed 's/^# \{0,1\}//'; exit 1 ;;
esac
