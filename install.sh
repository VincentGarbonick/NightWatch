#!/usr/bin/env bash
# Optional: adds a `nightwatch` command and an app-menu entry for the current user.
# Nothing outside ~/.local is touched. Remove with:
#   rm ~/.local/bin/nightwatch ~/.local/share/applications/nightwatch.desktop
set -euo pipefail
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

missing=()
for c in ffmpeg ffprobe; do command -v "$c" >/dev/null || missing+=("$c"); done
python3 -c 'import cv2, numpy' 2>/dev/null || missing+=("python-opencv/python-numpy")
python3 -c 'import PySide6' 2>/dev/null || missing+=("pyside6")
if ((${#missing[@]})); then
  echo "Missing: ${missing[*]}"
  echo "Install with: sudo pacman -S --needed ffmpeg python-numpy python-opencv pyside6 mpv"
  exit 1
fi

chmod +x "$DIR/nightwatch.py"
mkdir -p "$HOME/.local/bin" "$HOME/.local/share/applications"
ln -sf "$DIR/nightwatch.py" "$HOME/.local/bin/nightwatch"
cat > "$HOME/.local/share/applications/nightwatch.desktop" <<EOF
[Desktop Entry]
Type=Application
Name=Nightwatch
GenericName=CCTV event finder
Comment=Find cars, people and lights in dark CCTV recordings
Exec=$DIR/nightwatch.py %F
Icon=camera-video
Terminal=false
Categories=AudioVideo;Video;
MimeType=video/mp2t;video/mp4;video/x-matroska;
EOF
command -v update-desktop-database >/dev/null && update-desktop-database "$HOME/.local/share/applications" || true
echo "Installed. Run 'nightwatch' (GUI), 'nightwatch scan ...' (CLI), or open Nightwatch from your app menu."
case ":$PATH:" in *":$HOME/.local/bin:"*) ;; *) echo "Note: ~/.local/bin is not on your PATH.";; esac
