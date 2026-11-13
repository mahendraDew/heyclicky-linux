#!/usr/bin/env bash
# Install heyclicky for the current user.
#
#   ./install.sh                # app + desktop entry in ~/.local
#   ./install.sh --autostart    # also start Clicky when you log in
#   ./install.sh --skip-deps    # do not check/install system packages
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
AUTOSTART=0
SKIP_DEPS=0
for arg in "$@"; do
  case "$arg" in
    --autostart) AUTOSTART=1 ;;
    --skip-deps) SKIP_DEPS=1 ;;
    -h|--help)
      sed -n '2,8p' "$0"
      exit 0
      ;;
    *)
      echo "unknown option: $arg" >&2
      exit 1
      ;;
  esac
done

if ! command -v python3 >/dev/null 2>&1; then
  echo "python3 is required. Install it with: sudo apt install python3 python3-venv" >&2
  exit 1
fi

PY_VERSION="$(python3 -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")')"
if ! python3 -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 10) else 1)'; then
  echo "Python 3.10 or newer is required (found $PY_VERSION)." >&2
  exit 1
fi

if [[ "$SKIP_DEPS" -eq 0 ]]; then
  missing=()
  for command in arecord ffplay gsettings; do
    command -v "$command" >/dev/null 2>&1 || missing+=("$command")
  done
  if ((${#missing[@]})); then
    echo "Optional system tools missing: ${missing[*]}"
    echo "Install them with:"
    echo "  sudo apt install alsa-utils ffmpeg libxcb-cursor0"
    echo "Continuing; you can install them later."
  fi
  if command -v gst-inspect-1.0 >/dev/null 2>&1; then
    gst-inspect-1.0 pipewiresrc >/dev/null 2>&1 || {
      echo "Wayland screen sharing needs the PipeWire GStreamer plugin:"
      echo "  sudo apt install python3-gi gstreamer1.0-pipewire"
    }
  else
    echo "Wayland screen sharing needs PyGObject and GStreamer:"
    echo "  sudo apt install python3-gi gstreamer1.0-pipewire"
  fi
  if [ -x /usr/bin/python3 ] && ! /usr/bin/python3 -c 'import gi' >/dev/null 2>&1; then
    echo "Wayland screen sharing needs PyGObject:"
    echo "  sudo apt install python3-gi"
  fi
fi

echo "==> creating virtualenv"
python3 -m venv "$ROOT/.venv"
echo "==> installing heyclicky (editable)"
"$ROOT/.venv/bin/pip" install --upgrade pip >/dev/null
"$ROOT/.venv/bin/pip" install -e "$ROOT"

BIN_DIR="$HOME/.local/bin"
APP_DIR="$HOME/.local/share/applications"
ICON_DIR="$HOME/.local/share/icons/hicolor/scalable/apps"
AUTOSTART_DIR="$HOME/.config/autostart"
mkdir -p "$BIN_DIR" "$APP_DIR" "$ICON_DIR"

EXEC="$BIN_DIR/heyclicky"
ln -sf "$ROOT/.venv/bin/heyclicky" "$EXEC"
cp "$ROOT/packaging/heyclicky.svg" "$ICON_DIR/heyclicky.svg"

sed "s|@EXEC@|$EXEC|g" "$ROOT/packaging/heyclicky.desktop.in" > "$APP_DIR/heyclicky.desktop"
chmod 644 "$APP_DIR/heyclicky.desktop" "$ICON_DIR/heyclicky.svg"

if command -v update-desktop-database >/dev/null 2>&1; then
  update-desktop-database "$APP_DIR" >/dev/null 2>&1 || true
fi
if command -v gtk-update-icon-cache >/dev/null 2>&1; then
  gtk-update-icon-cache -f -t "$HOME/.local/share/icons/hicolor" >/dev/null 2>&1 || true
fi

if [[ "$AUTOSTART" -eq 1 ]]; then
  mkdir -p "$AUTOSTART_DIR"
  sed "s|@EXEC@|$EXEC|g" "$ROOT/packaging/heyclicky-autostart.desktop.in" > "$AUTOSTART_DIR/heyclicky.desktop"
  echo "==> autostart enabled (~/.config/autostart/heyclicky.desktop)"
fi

if ! command -v heyclicky >/dev/null 2>&1; then
  echo
  echo "note: $BIN_DIR is not on your PATH. Add it with:"
  echo '  echo '\''export PATH="$HOME/.local/bin:$PATH"'\'' >> ~/.profile'
fi

echo
echo "Installed. Start it with:"
echo "  heyclicky            # or open Clicky from your app grid"
echo "  heyclicky --doctor   # check providers, audio, capture and hotkeys"
echo
echo "On Wayland, grant input access for true push-to-talk:"
echo "  sudo usermod -aG input \$USER   # then log out and back in"
