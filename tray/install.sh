#!/bin/sh
# Adds "Nerd Dictation" to the applications menu, pointing at this checkout.
set -e
DIR="$(cd "$(dirname "$0")" && pwd)"
mkdir -p "$HOME/.local/share/applications"
cat > "$HOME/.local/share/applications/nerd-dictation-tray.desktop" <<DESKTOP
[Desktop Entry]
Type=Application
Name=Nerd Dictation
Comment=Offline voice typing (tray icon)
Exec=$DIR/nerd-dictation-tray
Icon=audio-input-microphone
Categories=Utility;Accessibility;
Terminal=false
DESKTOP
echo "Installed menu entry for $DIR/nerd-dictation-tray"
