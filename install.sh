#!/usr/bin/env bash
# Installs niri-gesture-suite for the current user.
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
    if "niri-gesture-suite" in name.group(1):
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
install -Dm755 niri_gesture_suite.py "$HOME/.local/bin/niri-gesture-suite"
if [[ ! -e "$HOME/.config/niri-gesture-suite/config.toml" ]]; then
    install -Dm644 config.toml "$HOME/.config/niri-gesture-suite/config.toml"
    echo "Installed default config to ~/.config/niri-gesture-suite/config.toml"
else
    echo "Keeping existing ~/.config/niri-gesture-suite/config.toml"
fi
install -Dm644 niri-gesture-suite.service \
    "$HOME/.config/systemd/user/niri-gesture-suite.service"

# --- udev rules (needs sudo once) ------------------------------------------
RULE="/etc/udev/rules.d/70-niri-gesture-suite.rules"
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
systemctl --user enable --now niri-gesture-suite.service
sleep 1
systemctl --user --no-pager --lines=5 status niri-gesture-suite.service || true
echo
echo "Done. Try a 3-finger swipe on the screen."
