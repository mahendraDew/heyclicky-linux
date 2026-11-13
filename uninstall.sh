#!/usr/bin/env bash
# Remove the user-level desktop integration (config is kept).
set -euo pipefail

rm -f "$HOME/.local/bin/heyclicky"
rm -f "$HOME/.local/share/applications/heyclicky.desktop"
rm -f "$HOME/.local/share/icons/hicolor/scalable/apps/heyclicky.svg"
rm -f "$HOME/.config/autostart/heyclicky.desktop"

if command -v update-desktop-database >/dev/null 2>&1; then
  update-desktop-database "$HOME/.local/share/applications" >/dev/null 2>&1 || true
fi

echo "Removed desktop entry, icon and launcher."
echo "Your config is still at ${XDG_CONFIG_HOME:-$HOME/.config}/heyclicky/config.json"
echo "Remove it manually if you also want to delete API keys and preferences."
