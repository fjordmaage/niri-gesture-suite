#!/usr/bin/env bash
set -euo pipefail
systemctl --user disable --now niri-gesture-suite.service 2>/dev/null || true
rm -f "$HOME/.config/systemd/user/niri-gesture-suite.service"
rm -f "$HOME/.local/bin/niri-gesture-suite"
systemctl --user daemon-reload
echo "Removed service and binary. Config kept in ~/.config/niri-gesture-suite."
echo "To remove the udev rule too:"
echo "  sudo rm /etc/udev/rules.d/70-niri-gesture-suite.rules && sudo udevadm control --reload"
