# niri-gesture-suite

A full multi-finger gesture suite for the [niri](https://github.com/YaLTeR/niri)
compositor, on both the **touchscreen** and the **trackpad**: native 1:1
swipes, window move/resize by holding, pinch gestures, taps, media
controls, and screen-edge faders.

(The minimal swipe-only ancestor of this project lives on as
`niri-touch-gestures`.)

| Gesture (finger motion) | Action |
|---|---|
| 3-finger swipe left / right | scroll through columns |
| 3-finger swipe up / down | switch workspaces |
| 4-finger swipe up / down | open / close Overview |
| 3-finger hold on a window | pick up and move the window (Mod+drag); drag to top/bottom edge to carry it across workspaces |
| 3-finger pinch in on a window | close that window |
| 3-finger spread out | open a terminal |
| 4-finger spread / pinch | staged window zoom: partial column ⇄ maximized column ⇄ maximized to edges ⇄ fullscreen |
| 4-finger hold on a window | resize the window (Mod+right-drag) |
| 3-finger double tap | toggle floating |
| 5-finger tap | open the launcher |
| 4-finger tap | play / pause |
| 4-finger swipe left / right | next / previous track |
| 5-finger pinch | screenshot UI |
| 1-finger vertical drag on the right / left screen edge | volume / brightness as an absolute fader (80 mm = full range); edge taps still reach apps |

In the default **native mode** these are niri's *own* continuous gestures:
the view moves 1:1 with your fingers and, on release, completes or snaps back
with your release velocity — identical physics and identical physical travel
to the trackpad. Normal 1- and 2-finger touch (taps, scrolling, pinch) keeps
working exactly as before.

The extra gestures also run on the **trackpad**: pinches, 4/5-finger taps, 4-finger horizontal media swipes,
and hold-to-move/resize at the mouse cursor (pinch-close targets the window
under the cursor via a compositor-consumed Mod+click). Native niri
3/4-finger trackpad swipes pass through live with **zero added latency** —
since 3+ finger touchpad input never reaches apps, everything is forwarded
to the clone immediately while the daemon classifies in parallel; a pinch,
hold, or tap that claims the gesture cancels the clone's contacts, ending
whatever libinput had tentatively begun.

## Design

Inspired by how Apple approached the touchscreen Mac: don't invent a new
gesture language for the screen — carry the trackpad's over, but anchor it to
**direct manipulation**. On a trackpad you move an invisible viewport; on a
screen your fingers are *on* the content, so the content follows them:

- Swipe **left** → the canvas slides left → you land on the column to the
  **right**. (Same muscle memory as natural-scroll trackpad swipes.)
- Swipe **down** → the workspace stack slides down → you land on the workspace
  **above**.

Rather than re-implementing niri's gesture physics, native mode feeds the
gesture back through the same pipeline the trackpad uses: the daemon mirrors
your 3/4 fingers onto a **virtual touchpad**, libinput recognizes a native
multi-finger swipe, and niri drives its continuous gesture — tracking while
fingers are down, velocity-based completion on release, rubber-banding on
partial swipes. Your niri `input.touchpad` settings (like `natural-scroll`)
apply to it the same way.

The mirror's gain is **auto-calibrated for true direct manipulation**: the
content moves exactly as far as your fingers. niri needs 300 libinput units
for a workspace switch and 1200 for a working-area width of view scroll
(`src/layout/monitor.rs`, `scrolling.rs`), and libinput's unaccelerated
touchpad deltas are `mm × (1000/25.4) × slowdown`, where the slowdown depends
on the accel profile (`0.9 × 0.2968` adaptive; `0.2968 × (1 + accel-speed)`
flat) — the daemon reads your accel profile/speed from the niri config and
solves for the gain that pins the content under your fingertips. The
4-finger Overview zoom keeps trackpad-equivalent travel instead (it is not a
drag; `scale_overview` tunes it).

**Window gestures**: holding 3 fingers still for `hold_ms` performs a real
interactive move — the daemon warps a virtual absolute pointer to your
fingers, holds a virtual Mod key, and presses the left button, so niri's own
Mod+drag picks up the window under your fingers; it follows them (even if you
lift down to one finger) and drops on release, anywhere, including onto other
workspaces in the Overview. Pinching 3 fingers together by `pinch_close_mm`
closes the window under the fingers: the daemon hit-tests the touch point
against `niri msg windows` tile rectangles (`tile_pos_in_workspace_view` +
`tile_size`, preferring floating windows) and issues
`close-window --id <id>` — falling back to the focused window if the
hit-test fails.

There is also a **discrete mode** (`mode = "discrete"`) where the daemon
interprets swipes itself and fires configurable `niri msg action …` steps —
useful for arbitrary mappings (e.g. 4-finger horizontal). Thresholds are
physical millimeters: ~28 mm for the first step, ~36 mm per further step,
plus fast-flick detection (≥ 8 mm at ≥ 130 mm/s).

## How it works

A small Python daemon (only dependency: `python-evdev`) grabs the touchscreen
with `EVIOCGRAB` and re-emits everything through a virtual uinput clone that
niri reads instead. 1–2 finger input is forwarded verbatim with no added
latency. The moment a 3rd finger lands, the already-forwarded touches are
cancelled on the clone (so the app under your fingers never sees a stray tap
or scroll); once the fingers travel past a small deadzone, they are replayed
onto the virtual touchpad (native mode) or interpreted into IPC actions
(discrete mode). Contacts are recentered on the oversized virtual pad so they
never start in libinput's edge/palm zones. Multitouch protocol-B slot caches
are kept in sync across swallowed gestures, and screen rotation is picked up
from `niri msg outputs` automatically.

If the daemon stops, the grab is released and the touchscreen falls back to
plain niri handling — it can never leave you without touch.

## Install (Arch)

```bash
sudo pacman -S --needed python-evdev
./install.sh
```

`install.sh` installs the daemon to `~/.local/bin/niri-gesture-suite`, a
config to `~/.config/niri-gesture-suite/config.toml`, and a systemd user
service, then (the one `sudo` step) adds a udev rule that `uaccess`-tags the
touchscreen so the logged-in user can read it — scoped to that one device,
instead of joining the `input` group and gaining access to every keyboard.

## Configuration

See [config.toml](config.toml) — device selection, rotation override, all
thresholds in mm, and the direction → action map. Directions refer to the
motion of your fingers; any `niri msg action …` is accepted, `""` disables a
direction. After editing:

```bash
systemctl --user restart niri-gesture-suite
```

## Try it / debug

```bash
# watch decisions without touching niri (run while the service is stopped):
systemctl --user stop niri-gesture-suite
niri-gesture-suite --dry-run -v
# logs:
journalctl --user -u niri-gesture-suite -f
```

## Troubleshooting

- **"no touchscreen found, retrying"** — the udev rule hasn't been applied
  yet; re-run `install.sh` or `sudo udevadm trigger --subsystem-match=input`.
- **Swipes act rotated** after rotating the screen with a forced transform:
  set `transform` in the config instead of `"auto"`.
- **Steps feel too long/short** — tune `trigger_mm` / `repeat_mm`; they are
  real millimeters, measure against your trackpad habit.
- **Pen input** is a separate device and is not touched.

## Uninstall

```bash
./uninstall.sh
```
