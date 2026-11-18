#!/usr/bin/env bash
# Installs niri-touch-gestures for the current user.
# The only privileged step is a udev rule that lets the logged-in seat user
# read the touchscreen (TAG+="uaccess") — safer than joining the input group.
set -euo pipefail
cd "$(dirname "$0")"

# --- detect the touchscreen name (direct-touch multitouch device) ----------
TOUCH_NAME="$(python3 - <<'EOF'
import re
blocks = open("/proc/bus/input/devices").read().strip().split("\n\n")
for b in blocks:
    name = re.search(r'^N: Name="(.*)"', b, re.M)
    prop = re.search(r'^B: PROP=([0-9a-f ]+)$', b, re.M)
    absb = re.search(r'^B: ABS=([0-9a-f ]+)$', b, re.M)
    if not (name and prop and absb):
        continue
    def bits(s):
        v = 0
        for w in s.split():
            v = (v << 64) | int(w, 16)
        return v
    if "niri-touch-gestures" in name.group(1):
        continue
    if bits(prop.group(1)) & 0x2 and bits(absb.group(1)) & (1 << 0x2f):
        print(name.group(1))
        break
EOF
)"
if [[ -z "$TOUCH_NAME" ]]; then
    echo "ERROR: no touchscreen (direct multitouch device) found." >&2
    exit 1
fi
echo "Touchscreen: $TOUCH_NAME"

# --- user-level files -------------------------------------------------------
install -Dm755 niri_touch_gestures.py "$HOME/.local/bin/niri-touch-gestures"
if [[ ! -e "$HOME/.config/niri-touch-gestures/config.toml" ]]; then
    install -Dm644 config.toml "$HOME/.config/niri-touch-gestures/config.toml"
    echo "Installed default config to ~/.config/niri-touch-gestures/config.toml"
else
    echo "Keeping existing ~/.config/niri-touch-gestures/config.toml"
fi
install -Dm644 niri-touch-gestures.service \
    "$HOME/.config/systemd/user/niri-touch-gestures.service"

# --- udev rule (needs sudo once) -------------------------------------------
RULE="/etc/udev/rules.d/70-niri-touch-gestures.rules"
RULE_TEXT="SUBSYSTEM==\"input\", KERNEL==\"event*\", ATTRS{name}==\"$TOUCH_NAME\", TAG+=\"uaccess\""
if [[ ! -e "$RULE" ]] || ! grep -qF "$RULE_TEXT" "$RULE" 2>/dev/null; then
    echo "Installing udev rule (sudo): $RULE"
    echo "$RULE_TEXT" | sudo tee "$RULE" >/dev/null
    sudo udevadm control --reload
    sudo udevadm trigger --subsystem-match=input
else
    echo "udev rule already in place."
fi

# --- enable -----------------------------------------------------------------
systemctl --user daemon-reload
systemctl --user enable --now niri-touch-gestures.service
sleep 1
systemctl --user --no-pager --lines=5 status niri-touch-gestures.service || true
echo
echo "Done. Try a 3-finger swipe on the screen."
