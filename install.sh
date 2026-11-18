#!/usr/bin/env bash
# Installs niri-touch-gestures for the current user.
# The only privileged step is a udev rule that lets the logged-in seat user
# read the touchscreen (TAG+="uaccess") — safer than joining the input group.
set -euo pipefail
cd "$(dirname "$0")"

# --- detect touchscreen + touchpad names (multitouch devices) ---------------
mapfile -t TOUCH_NAMES < <(python3 - <<'EOF'
import re
blocks = open("/proc/bus/input/devices").read().strip().split("\n\n")
ts = tp = None
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
    if not bits(absb.group(1)) & (1 << 0x2f):
        continue  # not multitouch
    p = bits(prop.group(1))
    if p & 0x2 and ts is None:       # INPUT_PROP_DIRECT: touchscreen
        ts = name.group(1)
    elif p & 0x1 and tp is None:     # INPUT_PROP_POINTER: touchpad
        tp = name.group(1)
for n in (ts, tp):
    if n:
        print(n)
EOF
)
if [[ ${#TOUCH_NAMES[@]} -eq 0 ]]; then
    echo "ERROR: no touchscreen or touchpad (multitouch device) found." >&2
    exit 1
fi
printf 'Devices: %s\n' "${TOUCH_NAMES[@]}"

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

# --- udev rules (needs sudo once) ------------------------------------------
RULE="/etc/udev/rules.d/70-niri-touch-gestures.rules"
RULE_TEXT=""
for name in "${TOUCH_NAMES[@]}"; do
    RULE_TEXT+="SUBSYSTEM==\"input\", KERNEL==\"event*\", ATTRS{name}==\"$name\", TAG+=\"uaccess\""$'\n'
done
if [[ ! -e "$RULE" ]] || [[ "$(cat "$RULE" 2>/dev/null)" != "$(printf '%s' "$RULE_TEXT")" ]]; then
    echo "Installing udev rules (sudo): $RULE"
    printf '%s' "$RULE_TEXT" | sudo tee "$RULE" >/dev/null
    sudo udevadm control --reload
    sudo udevadm trigger --subsystem-match=input
else
    echo "udev rules already in place."
fi

# --- enable -----------------------------------------------------------------
systemctl --user daemon-reload
systemctl --user enable --now niri-touch-gestures.service
sleep 1
systemctl --user --no-pager --lines=5 status niri-touch-gestures.service || true
echo
echo "Done. Try a 3-finger swipe on the screen."
