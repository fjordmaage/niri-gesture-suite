#!/usr/bin/env bash
set -euo pipefail
systemctl --user disable --now niri-touch-gestures.service 2>/dev/null || true
rm -f "$HOME/.config/systemd/user/niri-touch-gestures.service"
rm -f "$HOME/.local/bin/niri-touch-gestures"
systemctl --user daemon-reload
echo "Removed service and binary. Config kept in ~/.config/niri-touch-gestures."
echo "To remove the udev rule too:"
echo "  sudo rm /etc/udev/rules.d/70-niri-touch-gestures.rules && sudo udevadm control --reload"
