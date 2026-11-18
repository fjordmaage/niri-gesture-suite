# niri-touch-gestures

Multi-finger **touchscreen** gestures for the [niri](https://github.com/YaLTeR/niri)
compositor — the same gestures you already use on the trackpad, on the glass.

| Gesture (finger motion) | Action |
|---|---|
| 3-finger swipe left / right | focus column right / left |
| 3-finger swipe up / down | focus workspace below / above |
| 4-finger swipe up | open Overview |
| 4-finger swipe down | close Overview |

Normal 1- and 2-finger touch (taps, scrolling, pinch) keeps working exactly as
before.

## Design

Inspired by how Apple approached the touchscreen Mac: don't invent a new
gesture language for the screen — carry the trackpad's over, but anchor it to
**direct manipulation**. On a trackpad you move an invisible viewport; on a
screen your fingers are *on* the content, so the content follows them:

- Swipe **left** → the canvas slides left → you land on the column to the
  **right**. (Same muscle memory as natural-scroll trackpad swipes.)
- Swipe **down** → the workspace stack slides down → you land on the workspace
  **above**.

All thresholds are physical **millimeters on the glass** (using the
touchscreen's reported resolution), so a workspace switch needs about the same
finger travel as on the trackpad: ~28 mm for the first step, then ~36 mm per
additional step while you keep swiping — one long swipe crosses several
columns or workspaces, so it feels continuous rather than "one shot per
swipe". A short but fast **flick** (≥ 8 mm at ≥ 130 mm/s) also counts, so lazy
quick swipes still land. A 4-finger swipe up opens the Overview at ~22 mm; a
reversal within the same gesture closes it again.

## How it works

A small Python daemon (only dependency: `python-evdev`) grabs the touchscreen
with `EVIOCGRAB` and re-emits everything through a virtual uinput clone that
niri reads instead. 1–2 finger input is forwarded verbatim with no added
latency. The moment a 3rd finger lands, the already-forwarded touches are
cancelled on the clone (so the app under your fingers never sees a stray tap
or scroll) and the swipe is interpreted by the daemon, which drives niri over
its IPC (`niri msg action …`). Multitouch protocol-B slot caches are kept in
sync across swallowed gestures, and screen rotation is picked up from
`niri msg outputs` automatically.

If the daemon stops, the grab is released and the touchscreen falls back to
plain niri handling — it can never leave you without touch.

## Install (Arch)

```bash
sudo pacman -S --needed python-evdev
./install.sh
```

`install.sh` installs the daemon to `~/.local/bin/niri-touch-gestures`, a
config to `~/.config/niri-touch-gestures/config.toml`, and a systemd user
service, then (the one `sudo` step) adds a udev rule that `uaccess`-tags the
touchscreen so the logged-in user can read it — scoped to that one device,
instead of joining the `input` group and gaining access to every keyboard.

## Configuration

See [config.toml](config.toml) — device selection, rotation override, all
thresholds in mm, and the direction → action map. Directions refer to the
motion of your fingers; any `niri msg action …` is accepted, `""` disables a
direction. After editing:

```bash
systemctl --user restart niri-touch-gestures
```

## Try it / debug

```bash
# watch decisions without touching niri (run while the service is stopped):
systemctl --user stop niri-touch-gestures
niri-touch-gestures --dry-run -v
# logs:
journalctl --user -u niri-touch-gestures -f
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
