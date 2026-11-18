#!/usr/bin/env python3
"""niri-touch-gestures — multi-finger touchscreen gestures for the niri compositor.

Grabs the touchscreen (EVIOCGRAB), forwards 1-2 finger input verbatim to a
virtual clone device (so normal touch keeps working), and interprets 3- and
4-finger swipes as niri actions with millimeter-based travel thresholds, so
screen gestures feel like the trackpad ones.

Direction names in the config refer to the motion of the fingers on the
screen; the defaults implement direct manipulation (content follows fingers).
"""

import argparse
import glob
import json
import logging
import math
import os
import re
import select
import shlex
import signal
import subprocess
import sys
import time
from collections import deque
from pathlib import Path

try:
    import tomllib
except ModuleNotFoundError:  # Python < 3.11
    tomllib = None

import evdev
from evdev import AbsInfo, InputDevice, UInput, ecodes as ec

log = logging.getLogger("niri-touch-gestures")

VIRTUAL_NAME_SUFFIX = "niri-touch-gestures virtual"

DEFAULT_CONFIG = {
    "trackpad": {
        # Also run gestures on the trackpad: pinches, 4/5-finger taps,
        # 4-finger horizontal media swipes, and hold-to-move/resize at the
        # cursor. Native 3/4-finger niri swipes pass through untouched.
        "enabled": True,
        # Substring of the touchpad device name; empty = auto-detect.
        "name": "",
        # Cursor speed while dragging a window from the trackpad.
        "move_gain": 1.0,
        # Travel before a trackpad gesture classifies (mm). Smaller than the
        # touchscreen deadzone so native swipes engage with less latency.
        "deadzone_mm": 3.0,
        # Pinch thresholds on the trackpad (mm of mean contact distance
        # change). Much smaller than on the glass: fingers start cramped,
        # and accidental pinches are unlikely there.
        "pinch_close_mm": 6.0,
        "pinch_out_mm": 6.0,
    },
    "device": {
        # Substring or exact name of the touchscreen. Empty = auto-detect the
        # first direct-touch multitouch device.
        "name": "",
        # Output rotation: "auto" asks niri, or force "normal", "90", "180",
        # "270", "flipped", "flipped-90", "flipped-180", "flipped-270".
        "transform": "auto",
        # Used to convert travel to mm when the kernel driver does not report
        # a resolution for the touch axes.
        "fallback_width_mm": 285.0,
        "fallback_height_mm": 190.0,
    },
    "gestures": {
        # "native": mirror 3/4-finger touches onto a virtual touchpad so
        # niri's own continuous gesture handling takes over (1:1 tracking,
        # release velocity, rubber-banding — identical to the real trackpad).
        # "discrete": interpret swipes in the daemon and fire the [actions]
        # below in steps (allows arbitrary mappings).
        "mode": "native",
        # Native-mode gains. "auto" computes true direct manipulation: the
        # content moves exactly as far as your fingers (derived from the
        # screen's physical size, niri's gesture constants, and the libinput
        # accel profile/speed read from your niri config). A number overrides
        # the computed gain (1.0 = trackpad-equivalent finger travel).
        "scale_x": "auto",
        "scale_y": "auto",
        # The 4-finger Overview gesture is a zoom, not a drag, so 1:1 does
        # not apply; this is a plain multiplier on trackpad-equivalent
        # travel (higher = shorter swipe opens the Overview).
        "scale_overview": 1.0,
        # Travel before a swipe engages / its axis is decided (mm).
        "deadzone_mm": 6.0,
        # Hold 3 fingers for this long (ms) to pick up the window under
        # them and move it (4 fingers: resize), exactly like Mod+drag with
        # the mouse. 0 disables. Native mode only. Holds don't need to be
        # still: anything slower than a deliberate swipe counts as holding
        # (see hold_still_mm and swipe_min_speed_mm_s).
        "hold_ms": 180,
        # Fingers may wander this far (mm) without resetting the hold timer.
        "hold_still_mm": 6.0,
        # A swipe engages at deadzone_mm only when the fingers move at least
        # this fast (mm/s); slower motion engages at twice the deadzone.
        # Keeps slow drifting fingers from stealing the gesture from a hold.
        "swipe_min_speed_mm_s": 30.0,
        # While moving a window, parking the drag within this many mm of the
        # top/bottom screen edge switches to the workspace above/below
        # (after move_edge_delay_ms, repeating every move_edge_repeat_ms).
        # 0 disables.
        "move_edge_mm": 12.0,
        "move_edge_delay_ms": 250,
        "move_edge_repeat_ms": 600,
        # Multi-finger taps: all fingers down and up within tap_ms with no
        # movement. A second 3-finger tap within double_tap_ms fires the
        # three_double action.
        "tap_ms": 250,
        "double_tap_ms": 400,
        # 1-finger edge strips: a touch starting within strip_mm of the
        # left/right screen edge and dragging vertically (strip_arm_mm to
        # arm) slides brightness/volume as an absolute fader: strip_range_mm
        # of travel covers the full 0-100 range from the current level.
        # Taps and horizontal swipes in the zone are passed through to apps
        # after at most strip_timeout_ms. 0 disables.
        "strip_mm": 4.0,
        "strip_arm_mm": 5.0,
        "strip_range_mm": 80.0,
        "strip_timeout_ms": 250,
        # Pinch 3 fingers together by this many mm (mean contact distance
        # from the centroid) to trigger the in3 pinch action (close the
        # window under them). 0 disables. Native mode only.
        "pinch_close_mm": 12.0,
        # Spread fingers apart by this many mm to trigger the out3/out4
        # pinch actions. 0 disables. Native mode only.
        "pinch_out_mm": 12.0,
        # Modifier used for the window-move drag; must match your niri Mod.
        # One of "Super", "Alt", "Ctrl", "Shift".
        "move_modifier": "Super",
        # Travel for the first step of a 3-finger swipe (mm).
        "trigger_mm": 28.0,
        # Travel for each subsequent step in the same swipe (mm).
        "repeat_mm": 36.0,
        # Travel for the first step of a 4-finger swipe (mm).
        "overview_mm": 22.0,
        # A swipe that ends before reaching trigger_mm still fires once if it
        # travelled at least flick_min_mm at flick_speed_mm_s or faster.
        "flick_min_mm": 8.0,
        "flick_speed_mm_s": 130.0,
    },
    "actions": {
        # How to run actions; the action string is appended, split on spaces.
        "command": "niri msg action",
        # Directions = finger motion. Defaults are direct manipulation:
        # drag the canvas left -> see the column on the right.
        "three": {
            "left": "focus-column-right",
            "right": "focus-column-left",
            "up": "focus-workspace-down",
            "down": "focus-workspace-up",
        },
        "four": {
            "up": "open-overview",
            "down": "close-overview",
            "left": "",
            "right": "",
        },
        # Pinch gestures (native mode): in/out + finger count -> niri action.
        # The action targets the window under the fingers (close-window
        # directly by id; other actions via focus-window first).
        # window-zoom-in / window-zoom-out are built-ins that walk the
        # focused window through: partial column -> maximized column ->
        # maximized to edges -> fullscreen (and back).
        "pinch": {
            "in3": "close-window",
            "out3": "spawn kitty",
            "in4": "window-zoom-out",
            "out4": "window-zoom-in",
            "in5": "screenshot",
            "out5": "",
        },
        # Taps -> niri actions ("" disables).
        "tap": {
            "five": "spawn noctalia msg panel-toggle launcher",
            "four": "spawn noctalia msg media toggle",
            "three_double": "toggle-window-floating",
        },
        # 4-finger horizontal swipes -> actions (vertical stays the native
        # Overview gesture). Directions are finger motion.
        "swipe4": {
            "left": "spawn noctalia msg media previous",
            "right": "spawn noctalia msg media next",
        },
        # Edge strips: <side>_get prints the current level (percent or
        # 0..1 fraction); <side>_set receives the target level for {}.
        "strip": {
            "right_get": "wpctl get-volume @DEFAULT_AUDIO_SINK@",
            "right_set": "noctalia msg volume-set {}",
            "left_get": "brightnessctl -m",
            "left_set": "noctalia msg brightness-set {}",
        },
    },
}


# --------------------------------------------------------------------------
# config

def deep_merge(base, override):
    out = dict(base)
    for k, v in override.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def load_config(path):
    cfg = DEFAULT_CONFIG
    if path and Path(path).exists():
        if tomllib is None:
            log.warning("tomllib unavailable; ignoring config file")
        else:
            with open(path, "rb") as f:
                cfg = deep_merge(cfg, tomllib.load(f))
            log.info("loaded config %s", path)
    return cfg


# --------------------------------------------------------------------------
# gesture gain calibration
#
# niri (v26.04, src/layout/monitor.rs and scrolling.rs) maps unaccelerated
# libinput swipe deltas to gesture progress with fixed constants:
NIRI_WORKSPACE_MOVEMENT = 300.0   # delta units for one full workspace switch
NIRI_VIEW_MOVEMENT = 1200.0       # delta units to scroll one working-area width
# libinput (filter-touchpad*.c) normalizes touchpad deltas to units of
# mm * (1000 dpi / 25.4), then multiplies by a slowdown factor that depends
# on the configured accel profile:
LIBINPUT_UNITS_PER_INCH_MM = 1000.0 / 25.4
TP_MAGIC_SLOWDOWN = 0.2968
TP_ADAPTIVE_BASELINE = 0.9


def libinput_units_per_mm(profile, speed):
    """Unaccelerated libinput delta units per mm of finger travel."""
    if profile == "flat":
        return LIBINPUT_UNITS_PER_INCH_MM * TP_MAGIC_SLOWDOWN * \
            max(0.005, 1.0 + speed)
    # adaptive (default): the constant filter ignores the speed setting
    return LIBINPUT_UNITS_PER_INCH_MM * TP_ADAPTIVE_BASELINE * TP_MAGIC_SLOWDOWN


def parse_niri_touchpad_accel(path=None):
    """Best-effort read of accel-profile/accel-speed from the niri config."""
    if path is None:
        base = os.environ.get("XDG_CONFIG_HOME",
                              os.path.expanduser("~/.config"))
        path = os.path.join(base, "niri", "config.kdl")
    profile, speed = "adaptive", 0.0
    try:
        text = Path(path).read_text()
    except OSError:
        return profile, speed
    text = re.sub(r"/\*.*?\*/", "", text, flags=re.S)
    depth = 0
    tp_depth = None
    for line in text.splitlines():
        s = line.split("//")[0].strip()
        if s.startswith("/-"):
            s = ""
        if tp_depth is None and re.match(r"touchpad\s*\{", s):
            tp_depth = depth
        if tp_depth is not None:
            m = re.search(r'accel-profile\s+"(\w+)"', s)
            if m:
                profile = m.group(1)
            m = re.search(r"accel-speed\s+(-?[0-9.]+)", s)
            if m:
                try:
                    speed = float(m.group(1))
                except ValueError:
                    pass
        depth += s.count("{") - s.count("}")
        if tp_depth is not None and depth <= tp_depth:
            tp_depth = None
    return profile, speed


def parse_niri_gaps(path=None):
    """Best-effort read of the layout gap size from the niri config."""
    if path is None:
        base = os.environ.get("XDG_CONFIG_HOME",
                              os.path.expanduser("~/.config"))
        path = os.path.join(base, "niri", "config.kdl")
    try:
        text = Path(path).read_text()
    except OSError:
        return 16.0
    for line in text.splitlines():
        m = re.match(r"\s*gaps\s+([0-9.]+)", line.split("//")[0])
        if m:
            return float(m.group(1))
    return 16.0


def zoom_stage(tile_w, tile_h, out_w, out_h, gaps):
    """Size stage of a tiled window: 0 partial column, 1 maximized column
    (full working width minus gaps), 2 maximized to edges, 3 fullscreen."""
    if tile_w >= out_w - 2 and tile_h >= out_h - 2:
        return 3
    if tile_w >= out_w - 2:
        return 2
    if tile_w >= out_w - 3.5 * gaps - 2:
        return 1
    return 0


def zoom_action(zoom_in, stage, wid):
    """The niri action argv that moves one stage up or down, or None."""
    by_id = ["--id", str(wid)]
    if zoom_in:
        return {0: ["maximize-column"],
                1: ["maximize-window-to-edges"] + by_id,
                2: ["fullscreen-window"] + by_id}.get(stage)
    return {3: ["fullscreen-window"] + by_id,
            2: ["maximize-window-to-edges"] + by_id,
            1: ["maximize-column"]}.get(stage)


# --------------------------------------------------------------------------
# transforms

TRANSFORMS = {
    "normal": lambda dx, dy: (dx, dy),
    "90": lambda dx, dy: (dy, -dx),
    "180": lambda dx, dy: (-dx, -dy),
    "270": lambda dx, dy: (-dy, dx),
    "flipped": lambda dx, dy: (-dx, dy),
    "flipped-90": lambda dx, dy: (dy, dx),
    "flipped-180": lambda dx, dy: (dx, -dy),
    "flipped-270": lambda dx, dy: (-dy, -dx),
}

# Same transforms for absolute normalized coordinates in [0, 1].
ABS_TRANSFORMS = {
    "normal": lambda nx, ny: (nx, ny),
    "90": lambda nx, ny: (ny, 1 - nx),
    "180": lambda nx, ny: (1 - nx, 1 - ny),
    "270": lambda nx, ny: (1 - ny, nx),
    "flipped": lambda nx, ny: (1 - nx, ny),
    "flipped-90": lambda nx, ny: (ny, nx),
    "flipped-180": lambda nx, ny: (nx, 1 - ny),
    "flipped-270": lambda nx, ny: (1 - ny, 1 - nx),
}

MODIFIER_KEYS = {
    "super": ec.KEY_LEFTMETA,
    "alt": ec.KEY_LEFTALT,
    "ctrl": ec.KEY_LEFTCTRL,
    "shift": ec.KEY_LEFTSHIFT,
}

PTR_RANGE = 65535  # virtual absolute pointer axis range


def niri_transform_name(raw):
    """Map niri's JSON transform naming to our keys."""
    s = str(raw).strip().lower().replace("flipped180", "flipped-180")
    s = s.replace("_", "-").replace(" ", "-")
    if s in TRANSFORMS:
        return s
    # niri reports e.g. "Normal", "_90", "Flipped90"
    s = s.lstrip("-").lstrip("_")
    for key in ("flipped-90", "flipped-180", "flipped-270", "flipped",
                "90", "180", "270", "normal"):
        if s == key.replace("-", "") or s == key:
            return key
    return "normal"


class TransformCache:
    """Resolves the output transform, asking niri at most every 30 s."""

    def __init__(self, cfg_transform):
        self.cfg = cfg_transform
        self.cached = "normal"
        self.ts = 0.0

    def get(self):
        if self.cfg != "auto":
            return self.cfg
        now = time.monotonic()
        if now - self.ts > 30.0:
            self.ts = now
            try:
                out = subprocess.run(
                    ["niri", "msg", "-j", "outputs"],
                    capture_output=True, timeout=2, env=niri_env(),
                )
                data = json.loads(out.stdout)
                for info in data.values():
                    logical = info.get("logical")
                    if logical:
                        self.cached = niri_transform_name(
                            logical.get("transform", "normal"))
                        break
            except Exception as exc:  # noqa: BLE001 - keep daemon alive
                log.debug("could not query niri outputs: %s", exc)
        return self.cached


def niri_env():
    env = dict(os.environ)
    if "NIRI_SOCKET" not in env:
        run = env.get("XDG_RUNTIME_DIR", f"/run/user/{os.getuid()}")
        socks = sorted(glob.glob(os.path.join(run, "niri.*.sock")))
        if socks:
            env["NIRI_SOCKET"] = socks[-1]
    return env


# --------------------------------------------------------------------------
# gesture engine (pure logic; testable without devices)

FORWARD, GESTURE, DRAIN = "forward", "gesture", "drain"
STRIP_PENDING, STRIP = "strip_pending", "strip"


class GestureEngine:
    """Consumes evdev frames, forwards or interprets them.

    emit(events): forward a list of (type, code, value) to the virtual device
                  (the caller appends SYN_REPORT).
    act(fingers, direction): fingers is 3 or 4; direction in l/r/u/d words.
    """

    def __init__(self, cfg, emit, act, mm_x, mm_y, transform_getter,
                 emit_pad=None, pad_geom=None, calib=None,
                 emit_ptr=None, emit_kbd=None,
                 window_at=None, pinch_action=None, move_edge=None,
                 strip_get=None, strip_set=None):
        self.cfg = cfg
        self.emit = emit
        self.act = act
        self.mm_x = mm_x  # units per mm
        self.mm_y = mm_y
        self.get_transform = transform_getter
        # native mode: mirror gesture fingers onto a virtual touchpad
        self.emit_pad = emit_pad
        self.pad_geom = pad_geom  # {"cx","cy","xmax","ymax"} in pad raw units
        self.pad_tid = 0         # ever-increasing tracking id for the pad
        # calibration for computing the "auto" gains: libinput units/mm and
        # the screen's physical size in device orientation
        self.calib = calib or {"u": libinput_units_per_mm("adaptive", 0.0),
                               "w_mm": 100.0, "h_mm": 100.0}
        # window gestures (hold to move, pinch to close)
        self.emit_ptr = emit_ptr      # virtual absolute pointer
        self.emit_kbd = emit_kbd      # virtual modifier keyboard
        self.window_at = window_at    # (nx, ny) -> window id or None
        self.pinch_action = pinch_action  # (action str, window id or None)
        self.move_edge = move_edge    # ("up"|"down") -> () while dragging
        self.strip_get = strip_get    # ("left"|"right") -> level 0..100
        self.strip_set = strip_set    # ("left"|"right", level) -> ()
        self.last_tap3 = 0.0          # time of the last 3-finger tap
        # edge-strip state
        self.strip_side = None
        self.strip_buf = []
        self.strip_t0 = 0.0
        self.strip_start = None       # (x_mm, y_mm) logical at touchdown
        self.strip_ref = 0.0          # y_mm where the fader armed
        self.strip_base = 50.0        # level when the fader armed
        self.strip_val = None         # latest computed level
        self.strip_sent = None        # last level actually sent
        self.strip_last_send = 0.0
        self.move_key = MODIFIER_KEYS.get(
            str(cfg["gestures"].get("move_modifier", "Super")).lower(),
            ec.KEY_LEFTMETA)

        self.mode = FORWARD
        # Protocol B is stateful: receivers must cache per-slot values,
        # because the device only reports values that changed.
        self.down = {}           # slot -> tracking_id (fingers on screen)
        self.pos = {}            # slot -> [x, y], persistent cache (real dev)
        self.cur_slot = 0
        self.key_state = {}      # EV_KEY code -> value (mirrors clone)
        self.clone_pos = {}      # slot -> (x, y) as last written to clone
        self.clone_cur_slot = 0
        self.resync = True       # sync clone slot caches on next forward

        self._reset_gesture()

    # -- shadow state ------------------------------------------------------

    def _reset_gesture(self):
        self.axis = None         # 'h' or 'v'
        self.fingers = 0         # locked at axis decision (3 or 4)
        self.acc = [0.0, 0.0]    # accumulated centroid travel, mm, transformed
        self.ref = 0.0           # axis position of last fired step
        self.fired = False
        self.prev_centroid = None
        self.prev_set = frozenset()
        self.samples = deque()   # (t, acc_x, acc_y)
        self.transform = TRANSFORMS["normal"]
        # native-mode mirror state
        self.engaged = False
        self.centroid0 = None    # centroid (mm) when the mirror engaged
        self.sx = 1.0            # gain applied to mirrored x / y motion
        self.sy = 1.0
        self.engage_offset = (0.0, 0.0)  # replays pre-deadzone travel
        self.pad_map = {}        # real slot -> pad slot
        self.cur_tool = None     # BTN_TOOL_* currently reported on the pad
        self.transform_name = "normal"
        # pending-gesture disambiguation (native mode)
        self.sub = "pending"     # pending | swipe | move | done
        self.t0 = 0.0            # when the gesture started
        self.pinch_acc = 0.0     # accumulated spread change, mm (neg = pinch)
        self.r_prev = None
        self.prev_rset = frozenset()
        self.last_acc = (0.0, 0.0)
        self.ptr_pos = [0.0, 0.0]
        self.max_count = 0       # most fingers seen during this gesture
        self.last_norm = None    # last centroid in normalized logical coords
        self.move_btn = ec.BTN_LEFT
        # hold stillness tracking: holds fire after the fingers settle,
        # wherever they settled, not only at the landing spot
        self.settled = False
        self.still_anchor = (0.0, 0.0, 0.0)  # (acc_x, acc_y, pinch_acc)
        self.still_since = 0.0
        # workspace-switch edge zones during a window move
        self.zone_dir = None
        self.zone_since = 0.0
        self.zone_last = None

    def _apply(self, events):
        """Apply a frame to the shadow state of the real device."""
        for etype, code, value in events:
            if etype == ec.EV_ABS:
                if code == ec.ABS_MT_SLOT:
                    self.cur_slot = value
                elif code == ec.ABS_MT_TRACKING_ID:
                    if value == -1:
                        self.down.pop(self.cur_slot, None)
                    else:
                        self.down[self.cur_slot] = value
                elif code == ec.ABS_MT_POSITION_X:
                    self.pos.setdefault(self.cur_slot, [None, None])[0] = value
                elif code == ec.ABS_MT_POSITION_Y:
                    self.pos.setdefault(self.cur_slot, [None, None])[1] = value

    def _emit(self, events):
        """Send events to the clone, mirroring its per-slot position cache."""
        for etype, code, value in events:
            if etype == ec.EV_ABS:
                if code == ec.ABS_MT_SLOT:
                    self.clone_cur_slot = value
                elif code == ec.ABS_MT_POSITION_X:
                    x, y = self.clone_pos.get(self.clone_cur_slot, (None, None))
                    self.clone_pos[self.clone_cur_slot] = (value, y)
                elif code == ec.ABS_MT_POSITION_Y:
                    x, y = self.clone_pos.get(self.clone_cur_slot, (None, None))
                    self.clone_pos[self.clone_cur_slot] = (x, value)
            elif etype == ec.EV_KEY:
                self.key_state[code] = value
        self.emit(events)

    def _resync_prefix(self):
        """Bring the clone's slot position caches up to date with the real
        device's, then restore slot addressing. Needed after swallowed
        gesture frames: the device only reports changed values, relative to
        *its* cache, which has drifted from the clone's."""
        out = []
        for slot, (x, y) in sorted(self.pos.items()):
            cx, cy = self.clone_pos.get(slot, (None, None))
            if (x is not None and x != cx) or (y is not None and y != cy):
                out.append((ec.EV_ABS, ec.ABS_MT_SLOT, slot))
                if x is not None and x != cx:
                    out.append((ec.EV_ABS, ec.ABS_MT_POSITION_X, x))
                if y is not None and y != cy:
                    out.append((ec.EV_ABS, ec.ABS_MT_POSITION_Y, y))
        out.append((ec.EV_ABS, ec.ABS_MT_SLOT, self.cur_slot))
        return out

    def _centroid_mm(self):
        act = {s: self.pos[s] for s in self.down
               if s in self.pos and self.pos[s][0] is not None
               and self.pos[s][1] is not None}
        if not act:
            return None, frozenset()
        xs = sum(v[0] for v in act.values()) / len(act)
        ys = sum(v[1] for v in act.values()) / len(act)
        return (xs / self.mm_x, ys / self.mm_y), frozenset(act)

    # -- frame processing --------------------------------------------------

    def process_frame(self, events):
        pre_active = set(self.down)
        self._apply(events)
        count = len(self.down)

        if self.mode == FORWARD:
            if count >= 3:
                self._start_gesture(pre_active)
            elif count == 1 and not pre_active and self._strip_enter():
                self.strip_buf = [list(events)]
            else:
                self._forward(events)
        elif self.mode == STRIP_PENDING:
            self._strip_pending(events, count)
        elif self.mode == STRIP:
            self._strip_track(count)
        elif self.mode == GESTURE:
            self.max_count = max(self.max_count, count)
            if self.sub == "move":
                # a window drag keeps following even if fingers lift down
                # to one; it ends when the last finger leaves the glass
                if count == 0:
                    self._move_end()
                    self._reset_gesture()
                    self.mode = FORWARD
                    self.resync = True
                else:
                    self._track()
            elif count >= 3:
                self._track()
            else:
                self._finalize()
                self.mode = DRAIN if count > 0 else FORWARD
                if self.mode == FORWARD:
                    self.resync = True
        elif self.mode == DRAIN:
            if count == 0:
                self.mode = FORWARD
                self.resync = True

    def _forward(self, events):
        out = []
        if self.resync:
            out.extend(self._resync_prefix())
            self.resync = False
        out.extend((t, c, v) for t, c, v in events if t != ec.EV_SYN)
        if out:
            self._emit(out)

    def _start_gesture(self, pre_active):
        self.mode = GESTURE
        self._reset_gesture()
        self.t0 = time.monotonic()
        self.still_since = self.t0
        self.max_count = len(self.down)
        self.transform_name = self.get_transform()
        self.transform = TRANSFORMS.get(self.transform_name,
                                        TRANSFORMS["normal"])
        # Lift the touches the clone currently has down, so apps see the
        # 1-2 fingers that landed first as cancelled, not as a tap.
        out = []
        for slot in sorted(pre_active):
            out.append((ec.EV_ABS, ec.ABS_MT_SLOT, slot))
            out.append((ec.EV_ABS, ec.ABS_MT_TRACKING_ID, -1))
        for code, value in list(self.key_state.items()):
            if value:
                out.append((ec.EV_KEY, code, 0))
        if out:
            self._emit(out)
        self._track()

    def _track(self):
        g = self.cfg["gestures"]
        centroid, cur_set = self._centroid_mm()
        if centroid is None:
            return
        if self.prev_centroid is not None and cur_set == self.prev_set:
            dx = centroid[0] - self.prev_centroid[0]
            dy = centroid[1] - self.prev_centroid[1]
            tdx, tdy = self.transform(dx, dy)
            self.acc[0] += tdx
            self.acc[1] += tdy
        self.prev_centroid, self.prev_set = centroid, cur_set
        self.samples.append((time.monotonic(), self.acc[0], self.acc[1]))
        while len(self.samples) > 2 and \
                self.samples[-1][0] - self.samples[0][0] > 0.12:
            self.samples.popleft()

        if g.get("mode", "native") == "native" and self.emit_pad is not None:
            self._track_native(g)
        else:
            self._track_discrete(g)

    # -- native mode: mirror onto the virtual touchpad ---------------------

    def _gains(self, g):
        """Mirror gains for this gesture: (sx, sy)."""
        c = self.calib
        if len(self.down) >= 4:
            s = float(g.get("scale_overview", 1.0)) or 1.0
            return s, s
        # In rotated orientations, the logical width lies along the device's
        # y axis; the gains apply in logical (post-transform) space.
        rotated = self.transform_name in ("90", "270",
                                          "flipped-90", "flipped-270")
        w_mm = c["h_mm"] if rotated else c["w_mm"]
        h_mm = c["w_mm"] if rotated else c["h_mm"]
        sx_cfg = g.get("scale_x", "auto")
        sy_cfg = g.get("scale_y", "auto")
        sx = float(sx_cfg) if not isinstance(sx_cfg, str) \
            else NIRI_VIEW_MOVEMENT / (c["u"] * w_mm)
        sy = float(sy_cfg) if not isinstance(sy_cfg, str) \
            else NIRI_WORKSPACE_MOVEMENT / (c["u"] * h_mm)
        return sx, sy

    def _track_native(self, g):
        if self.sub == "swipe":
            self._pad_sync()
        elif self.sub == "move":
            self._move_update()
        elif self.sub == "pending":
            self._pending(g)
        # "done": swallow until all fingers lift

    def _pending(self, g):
        """Disambiguate swipe vs pinch-close vs hold-to-move."""
        r, rset = self._spread_mm()
        if r is not None:
            if self.r_prev is not None and rset == self.prev_rset:
                self.pinch_acc += r - self.r_prev
            self.r_prev, self.prev_rset = r, rset

        now = time.monotonic()
        # Settle grace: forgive the wiggle of three fingers landing, so a
        # hold doesn't need a perfectly clean touchdown.
        if not self.settled and now - self.t0 >= 0.1:
            self.settled = True
            if math.hypot(*self.acc) < g["deadzone_mm"] and \
                    abs(self.pinch_acc) < 4.0:
                self.acc = [0.0, 0.0]
                self.pinch_acc = 0.0
                self.still_anchor = (0.0, 0.0, 0.0)
        # Stillness anchor: a hold fires once the fingers stay roughly in
        # place for hold_ms, wherever they ended up.
        tol = float(g.get("hold_still_mm", 6.0) or 6.0)
        ax, ay, ap = self.still_anchor
        if math.hypot(self.acc[0] - ax, self.acc[1] - ay) > tol or \
                abs(self.pinch_acc - ap) > tol:
            self.still_anchor = (self.acc[0], self.acc[1], self.pinch_acc)
            self.still_since = now

        travel = math.hypot(*self.acc)
        n = len(self.down)
        self.last_norm = self._centroid_norm_logical() or self.last_norm
        pinch_in = float(g.get("pinch_close_mm", 0) or 0)
        pinch_out = float(g.get("pinch_out_mm", 0) or 0)

        if n in (3, 4, 5):
            # A pinch only needs to outrun 60% of the centroid travel, so
            # asymmetric pinches (anchored thumb) still fire promptly.
            if pinch_in > 0 and self.pinch_acc <= -pinch_in \
                    and -self.pinch_acc >= 0.6 * travel:
                self._pinch_fire(f"in{n}")
                return
            if pinch_out > 0 and self.pinch_acc >= pinch_out \
                    and self.pinch_acc >= 0.6 * travel:
                self._pinch_fire(f"out{n}")
                return
        # Swipes are quick, deliberate motions; slow drift stays holdable.
        speed = 0.0
        if len(self.samples) >= 2:
            t0s, x0s, y0s = self.samples[0]
            t1s, x1s, y1s = self.samples[-1]
            dt = max(t1s - t0s, 1e-4)
            speed = math.hypot(x1s - x0s, y1s - y0s) / dt
        fast = speed >= float(g.get("swipe_min_speed_mm_s", 30.0) or 0)
        if travel >= g["deadzone_mm"] and (fast or
                                           travel >= 2 * g["deadzone_mm"]) \
                and travel >= 1.2 * abs(self.pinch_acc):
            self._engage_swipe(g)
            return
        self._check_hold(g)

    def _engage_swipe(self, g):
        # Horizontal 4-finger swipes are media gestures (niri's own 4-finger
        # gesture is the vertical Overview one; horizontal does nothing).
        if len(self.down) == 4 and abs(self.acc[0]) > abs(self.acc[1]) \
                and self.pinch_action is not None:
            swipe4 = (self.cfg.get("actions") or {}).get("swipe4") or {}
            action = swipe4.get("left" if self.acc[0] < 0 else "right", "")
            if action:
                self.sub = "done"
                log.info("4-finger horizontal swipe -> %s", action)
                self.pinch_action(action, None)
                return
        self.sub = "swipe"
        self.engaged = True
        self.centroid0 = self.prev_centroid
        self.sx, self.sy = self._gains(g)
        self.fingers = len(self.down)
        log.debug("native gesture engaged (%d fingers, gain %.3f/%.3f)",
                  self.fingers, self.sx, self.sy)
        self._pad_sync()
        # Replay the travel spent inside the deadzone on the next frame,
        # so the content catches up to the finger instead of losing it.
        self.engage_offset = (self.acc[0], self.acc[1])

    def _check_hold(self, g):
        hold_ms = float(g.get("hold_ms", 0) or 0)
        if hold_ms <= 0 or self.emit_ptr is None or self.emit_kbd is None:
            return
        n = len(self.down)
        if n not in (3, 4):
            return
        if (time.monotonic() - self.still_since) * 1000.0 < hold_ms:
            return
        # 3-finger hold moves the window (Mod+left drag); 4-finger hold
        # resizes it (Mod+right drag).
        self._move_begin(ec.BTN_LEFT if n == 3 else ec.BTN_RIGHT)

    def tick(self):
        """Called periodically by the event loop; drives time-based behavior
        (holds, edge dwell) even when perfectly still fingers send no
        events."""
        if self.mode == STRIP_PENDING:
            timeout = float(self.cfg["gestures"].get(
                "strip_timeout_ms", 250) or 250) / 1000.0
            if time.monotonic() - self.strip_t0 >= timeout:
                self._strip_exit(replay=True)  # long press: give it to the app
            return
        if self.mode != GESTURE:
            return
        if self.sub == "pending":
            self._check_hold(self.cfg["gestures"])
        elif self.sub == "move":
            self._move_edge_check()

    # -- 1-finger edge strips (brightness / volume) ------------------------

    def _logical_size_mm(self):
        c = self.calib
        rotated = self.transform_name in ("90", "270",
                                          "flipped-90", "flipped-270")
        if rotated:
            return c["h_mm"], c["w_mm"]
        return c["w_mm"], c["h_mm"]

    def _contact_mm(self):
        """The single contact as logical-mm coordinates, or None."""
        norm = self._centroid_norm_logical()
        if norm is None:
            return None
        w_mm, h_mm = self._logical_size_mm()
        return norm[0] * w_mm, norm[1] * h_mm

    def _strip_enter(self):
        """If the new single touch starts in an edge strip, capture it."""
        g = self.cfg["gestures"]
        zone = float(g.get("strip_mm", 0) or 0)
        if zone <= 0 or self.strip_set is None:
            return False
        self.transform_name = self.get_transform()
        self.transform = TRANSFORMS.get(self.transform_name,
                                        TRANSFORMS["normal"])
        pos = self._contact_mm()
        if pos is None:
            return False
        w_mm, _ = self._logical_size_mm()
        if pos[0] <= zone:
            self.strip_side = "left"
        elif pos[0] >= w_mm - zone:
            self.strip_side = "right"
        else:
            return False
        self.mode = STRIP_PENDING
        self.strip_t0 = time.monotonic()
        self.strip_start = pos
        return True

    def _strip_replay(self):
        """Forward the buffered frames: the touch was meant for an app."""
        for frame in self.strip_buf:
            evs = [(t, c, v) for t, c, v in frame if t != ec.EV_SYN]
            if evs:
                self._emit(evs)
        self.strip_buf = []

    def _strip_exit(self, replay):
        if replay:
            self._strip_replay()
        else:
            self.strip_buf = []
            self.resync = True
        self.strip_side = None
        self.mode = FORWARD

    def _strip_pending(self, events, count):
        g = self.cfg["gestures"]
        self.strip_buf.append(list(events))
        if count == 0:
            self._strip_exit(replay=True)  # it was a tap: let the app see it
            return
        if count >= 2:
            self._strip_exit(replay=True)
            if count >= 3:
                self._start_gesture(set(self.down))
            return
        pos = self._contact_mm()
        if pos is None:
            return
        dx = pos[0] - self.strip_start[0]
        dy = pos[1] - self.strip_start[1]
        arm = float(g.get("strip_arm_mm", 5.0) or 5.0)
        if abs(dy) >= arm and abs(dy) > 2.0 * abs(dx):
            self.strip_buf = []
            self.strip_ref = pos[1]
            base = None
            if self.strip_get is not None:
                base = self.strip_get(self.strip_side)
            self.strip_base = base if base is not None else 50.0
            self.strip_val = None
            self.strip_sent = None
            self.strip_last_send = 0.0
            self.mode = STRIP
            log.info("edge strip armed: %s at %.0f%%",
                     self.strip_side, self.strip_base)
        elif abs(dx) >= arm:
            self._strip_exit(replay=True)  # horizontal: not a strip

    def _strip_track(self, count):
        if count == 0:
            self._strip_flush(final=True)
            self.strip_side = None
            self.mode = FORWARD
            self.resync = True
            return
        if count >= 2:
            self._strip_flush(final=True)
            self.strip_side = None
            self.mode = DRAIN
            return
        pos = self._contact_mm()
        if pos is None:
            return
        g = self.cfg["gestures"]
        rng = float(g.get("strip_range_mm", 80.0) or 80.0)
        d = self.strip_ref - pos[1]  # moving up is positive
        val = max(0.0, min(100.0, self.strip_base + d * 100.0 / rng))
        self.strip_val = int(round(val))
        self._strip_flush(final=False)

    def _strip_flush(self, final):
        """Send the fader level, rate-limited; always send the last value."""
        if self.strip_val is None or self.strip_val == self.strip_sent:
            return
        now = time.monotonic()
        if not final and now - self.strip_last_send < 0.05:
            return
        self.strip_sent = self.strip_val
        self.strip_last_send = now
        self.strip_set(self.strip_side, self.strip_val)

    def _spread_mm(self):
        """Mean distance of the contacts from their centroid, in mm."""
        act = {s: self.pos[s] for s in self.down
               if s in self.pos and self.pos[s][0] is not None
               and self.pos[s][1] is not None}
        if len(act) < 2:
            return None, frozenset()
        cx = sum(v[0] for v in act.values()) / len(act) / self.mm_x
        cy = sum(v[1] for v in act.values()) / len(act) / self.mm_y
        r = sum(math.hypot(v[0] / self.mm_x - cx, v[1] / self.mm_y - cy)
                for v in act.values()) / len(act)
        return r, frozenset(act)

    def _centroid_norm_logical(self):
        """Centroid as normalized [0,1] logical-output coordinates."""
        centroid, _ = self._centroid_mm()
        if centroid is None:
            return None
        nx = min(1.0, max(0.0, centroid[0] / self.calib["w_mm"]))
        ny = min(1.0, max(0.0, centroid[1] / self.calib["h_mm"]))
        f = ABS_TRANSFORMS.get(self.transform_name, ABS_TRANSFORMS["normal"])
        return f(nx, ny)

    # -- pinch in/out ------------------------------------------------------

    def _pinch_fire(self, key):
        actions = (self.cfg.get("actions") or {}).get("pinch") or {}
        action = actions.get(key, "")
        self.sub = "done"
        if not action or self.pinch_action is None:
            return
        pos = self._centroid_norm_logical()
        wid = self.window_at(*pos) if (self.window_at and pos) else None
        log.info("pinch %s -> %s (window %s)",
                 key, action, wid if wid else "focused")
        self.pinch_action(action, wid)

    # -- hold to move (virtual Mod+drag) -----------------------------------

    def _move_begin(self, button=ec.BTN_LEFT):
        pos = self._centroid_norm_logical()
        if pos is None:
            return
        self.sub = "move"
        self.move_btn = button
        self.fingers = len(self.down)
        self.ptr_pos = [pos[0] * PTR_RANGE, pos[1] * PTR_RANGE]
        self.last_acc = (self.acc[0], self.acc[1])
        # The warp, the modifier, and the button come from two different
        # virtual devices; without pauses libinput may process the click
        # before the modifier, turning the grab into a plain click.
        self.emit_ptr([(ec.EV_ABS, ec.ABS_X, int(round(self.ptr_pos[0]))),
                       (ec.EV_ABS, ec.ABS_Y, int(round(self.ptr_pos[1])))])
        time.sleep(0.04)
        self.emit_kbd([(ec.EV_KEY, self.move_key, 1)])
        time.sleep(0.04)
        self.emit_ptr([(ec.EV_KEY, button, 1)])
        log.info("window %s begin",
                 "move" if button == ec.BTN_LEFT else "resize")

    def _move_update(self):
        c = self.calib
        rotated = self.transform_name in ("90", "270",
                                          "flipped-90", "flipped-270")
        w_mm = c["h_mm"] if rotated else c["w_mm"]
        h_mm = c["w_mm"] if rotated else c["h_mm"]
        dx = self.acc[0] - self.last_acc[0]
        dy = self.acc[1] - self.last_acc[1]
        if not dx and not dy:
            return
        self.last_acc = (self.acc[0], self.acc[1])
        self.ptr_pos[0] = min(PTR_RANGE, max(
            0.0, self.ptr_pos[0] + dx / w_mm * PTR_RANGE))
        self.ptr_pos[1] = min(PTR_RANGE, max(
            0.0, self.ptr_pos[1] + dy / h_mm * PTR_RANGE))
        self.emit_ptr([(ec.EV_ABS, ec.ABS_X, int(round(self.ptr_pos[0]))),
                       (ec.EV_ABS, ec.ABS_Y, int(round(self.ptr_pos[1])))])
        self._move_edge_check()

    def _move_edge_check(self):
        """While dragging a window, dwelling near the top/bottom screen edge
        switches to the workspace above/below."""
        g = self.cfg["gestures"]
        zone_mm = float(g.get("move_edge_mm", 0) or 0)
        if zone_mm <= 0 or self.move_edge is None:
            return
        c = self.calib
        rotated = self.transform_name in ("90", "270",
                                          "flipped-90", "flipped-270")
        h_mm = c["w_mm"] if rotated else c["h_mm"]
        frac = zone_mm / h_mm
        ny = self.ptr_pos[1] / PTR_RANGE
        d = "up" if ny <= frac else "down" if ny >= 1.0 - frac else None
        now = time.monotonic()
        if d != self.zone_dir:
            self.zone_dir = d
            self.zone_since = now
            self.zone_last = None
            return
        if d is None:
            return
        delay = float(g.get("move_edge_delay_ms", 250) or 0) / 1000.0
        repeat = float(g.get("move_edge_repeat_ms", 600) or 0) / 1000.0
        if self.zone_last is None:
            if now - self.zone_since >= delay:
                self.zone_last = now
                log.info("move edge: workspace %s", d)
                self.move_edge(d)
        elif now - self.zone_last >= repeat:
            self.zone_last = now
            log.info("move edge: workspace %s", d)
            self.move_edge(d)

    def _move_end(self):
        self.emit_ptr([(ec.EV_KEY, self.move_btn, 0)])
        time.sleep(0.02)
        self.emit_kbd([(ec.EV_KEY, self.move_key, 0)])
        log.info("window move/resize end")

    def _to_pad(self, x_raw, y_raw):
        vx = x_raw / self.mm_x - self.centroid0[0]
        vy = y_raw / self.mm_y - self.centroid0[1]
        tx, ty = self.transform(vx, vy)
        tx += self.engage_offset[0]
        ty += self.engage_offset[1]
        px = int(round(self.pad_geom["cx"] + tx * self.sx * self.mm_x))
        py = int(round(self.pad_geom["cy"] + ty * self.sy * self.mm_y))
        return (max(0, min(self.pad_geom["xmax"], px)),
                max(0, min(self.pad_geom["ymax"], py)))

    @staticmethod
    def _tool_for(n):
        return {1: ec.BTN_TOOL_FINGER, 2: ec.BTN_TOOL_DOUBLETAP,
                3: ec.BTN_TOOL_TRIPLETAP, 4: ec.BTN_TOOL_QUADTAP,
                }.get(n, ec.BTN_TOOL_QUINTTAP)

    def _pad_sync(self):
        """Diff real contacts against the mirrored ones and update the pad."""
        out = []
        active = {s: self.pos[s] for s in self.down
                  if s in self.pos and self.pos[s][0] is not None
                  and self.pos[s][1] is not None}
        was_empty = not self.pad_map
        # removals
        for rs in [rs for rs in self.pad_map if rs not in active]:
            out.append((ec.EV_ABS, ec.ABS_MT_SLOT, self.pad_map.pop(rs)))
            out.append((ec.EV_ABS, ec.ABS_MT_TRACKING_ID, -1))
        # additions and moves (the kernel drops unchanged ABS values)
        used = set(self.pad_map.values())
        first_pos = None
        for rs in sorted(active):
            px, py = self._to_pad(*active[rs])
            if rs not in self.pad_map:
                if len(self.pad_map) >= 6:
                    continue  # pad has 6 slots; ignore further contacts
                ps = next(i for i in range(6) if i not in used)
                used.add(ps)
                self.pad_map[rs] = ps
                self.pad_tid += 1
                out.append((ec.EV_ABS, ec.ABS_MT_SLOT, ps))
                out.append((ec.EV_ABS, ec.ABS_MT_TRACKING_ID, self.pad_tid))
            else:
                out.append((ec.EV_ABS, ec.ABS_MT_SLOT, self.pad_map[rs]))
            out.append((ec.EV_ABS, ec.ABS_MT_POSITION_X, px))
            out.append((ec.EV_ABS, ec.ABS_MT_POSITION_Y, py))
            if first_pos is None:
                first_pos = (px, py)
        # single-touch emulation + finger-count tools
        if first_pos is not None:
            out.append((ec.EV_ABS, ec.ABS_X, first_pos[0]))
            out.append((ec.EV_ABS, ec.ABS_Y, first_pos[1]))
        if was_empty and self.pad_map:
            out.append((ec.EV_KEY, ec.BTN_TOUCH, 1))
        tool = self._tool_for(len(self.pad_map)) if self.pad_map else None
        if tool != self.cur_tool:
            if self.cur_tool is not None:
                out.append((ec.EV_KEY, self.cur_tool, 0))
            if tool is not None:
                out.append((ec.EV_KEY, tool, 1))
            self.cur_tool = tool
        if out:
            self.emit_pad(out)

    def _release_pad(self):
        out = []
        for ps in self.pad_map.values():
            out.append((ec.EV_ABS, ec.ABS_MT_SLOT, ps))
            out.append((ec.EV_ABS, ec.ABS_MT_TRACKING_ID, -1))
        if self.cur_tool is not None:
            out.append((ec.EV_KEY, self.cur_tool, 0))
        out.append((ec.EV_KEY, ec.BTN_TOUCH, 0))
        self.emit_pad(out)
        log.debug("native gesture released")

    # -- discrete mode: interpret swipes ourselves -------------------------

    def _track_discrete(self, g):
        if self.axis is None:
            if math.hypot(*self.acc) >= g["deadzone_mm"]:
                self.axis = "h" if abs(self.acc[0]) >= abs(self.acc[1]) else "v"
                self.fingers = 3 if len(self.down) == 3 else 4
                log.debug("axis lock: %s, %d fingers", self.axis, self.fingers)
            else:
                return

        a = self.acc[0] if self.axis == "h" else self.acc[1]
        first = g["overview_mm"] if self.fingers >= 4 else g["trigger_mm"]
        while True:
            thr = g["repeat_mm"] if self.fired else first
            d = a - self.ref
            if abs(d) < thr:
                break
            sign = 1 if d > 0 else -1
            self.ref += sign * thr
            self.fired = True
            self.act(self.fingers, self._direction(sign))

    def _direction(self, sign):
        if self.axis == "h":
            return "right" if sign > 0 else "left"
        return "down" if sign > 0 else "up"

    def _check_taps(self, g):
        """Multi-finger taps, decided when the gesture ends unclaimed."""
        if self.pinch_action is None or \
                g.get("mode", "native") != "native":
            return
        now = time.monotonic()
        if (now - self.t0) * 1000.0 > float(g.get("tap_ms", 250) or 250):
            return
        if math.hypot(*self.acc) >= g["deadzone_mm"] or \
                abs(self.pinch_acc) >= 5.0:
            return
        taps = (self.cfg.get("actions") or {}).get("tap") or {}
        if self.max_count >= 5:
            action = taps.get("five", "")
            if action:
                log.info("5-finger tap -> %s", action)
                self.pinch_action(action, None)
        elif self.max_count == 4:
            action = taps.get("four", "")
            if action:
                log.info("4-finger tap -> %s", action)
                self.pinch_action(action, None)
        elif self.max_count == 3:
            if (now - self.last_tap3) * 1000.0 <= \
                    float(g.get("double_tap_ms", 400) or 400):
                self.last_tap3 = 0.0
                action = taps.get("three_double", "")
                if action:
                    wid = self.window_at(*self.last_norm) \
                        if (self.window_at and self.last_norm) else None
                    log.info("3-finger double tap -> %s (window %s)",
                             action, wid)
                    self.pinch_action(action, wid)
            else:
                self.last_tap3 = now

    def _finalize(self):
        g = self.cfg["gestures"]
        if self.engaged:
            self._release_pad()
        elif g.get("mode", "native") == "native" and self.emit_pad is not None:
            if self.sub == "pending":
                self._check_taps(g)
        elif self.axis is not None and not self.fired and len(self.samples) >= 2:
            a_now = self.acc[0] if self.axis == "h" else self.acc[1]
            t0, x0, y0 = self.samples[0]
            t1 = self.samples[-1][0]
            a_then = x0 if self.axis == "h" else y0
            dt = max(t1 - t0, 1e-4)
            speed = abs(a_now - a_then) / dt
            if abs(a_now) >= g["flick_min_mm"] and speed >= g["flick_speed_mm_s"]:
                self.act(self.fingers, self._direction(1 if a_now > 0 else -1))
        self._reset_gesture()


class TrackpadEngine(GestureEngine):
    """Gesture engine profile for a real touchpad.

    Unlike the touchscreen, niri's native 3/4-finger swipes must keep
    working, so 3+ finger frames are buffered until classified: a swipe
    replays the buffer to the clone verbatim (native gesture, tiny delay),
    while pinches, holds, and 4/5-finger taps are claimed - the already
    forwarded contacts are cancelled and the daemon acts. 1/2-finger input
    (pointer, scroll, taps) is forwarded untouched. Holds drive a virtual
    relative mouse, so the grab happens at the current cursor position.
    """

    def __init__(self, cfg, emit, mm_x, mm_y, emit_kbd=None, emit_rel=None,
                 pinch_action=None, calib=None, move_gain=1.0):
        super().__init__(
            cfg, emit, act=lambda n, d: None, mm_x=mm_x, mm_y=mm_y,
            transform_getter=lambda: "normal",
            emit_pad=(lambda evs: None),  # routes _track to the native path
            pad_geom=None, calib=calib,
            emit_ptr=emit_rel, emit_kbd=emit_kbd,
            window_at=None, pinch_action=pinch_action, move_edge=None,
            strip_get=None, strip_set=None,
        )
        self.emit_rel = emit_rel
        self.move_gain = move_gain
        self.relay_buf = []
        self.relay_pre = set()
        self.relay_claimed = False
        self.rel_rem = [0.0, 0.0]

    # -- relay plumbing ----------------------------------------------------

    def _relay_begin(self, pre_active, events):
        self.mode = GESTURE
        self._reset_gesture()
        self.t0 = time.monotonic()
        self.still_since = self.t0
        self.max_count = len(self.down)
        self.relay_buf = [list(events)]
        self.relay_pre = set(pre_active)
        self.relay_claimed = False
        self._track()

    def _relay_cancel(self):
        """Claim the gesture: lift the contacts the clone still has down."""
        out = []
        for slot in sorted(self.relay_pre):
            out.append((ec.EV_ABS, ec.ABS_MT_SLOT, slot))
            out.append((ec.EV_ABS, ec.ABS_MT_TRACKING_ID, -1))
        for code, value in list(self.key_state.items()):
            if value:
                out.append((ec.EV_KEY, code, 0))
        if out:
            self._emit(out)
        self.relay_buf = []
        self.relay_pre = set()
        self.relay_claimed = True
        self.resync = True

    def _relay_replay(self):
        """Not one of ours: hand the buffered frames to the clone."""
        if self.resync:
            # an earlier claimed gesture was swallowed; bring the clone's
            # per-slot position caches up to date first
            prefix = self._resync_prefix()
            if prefix:
                self._emit(prefix)
            self.resync = False
        # Pace the frames slightly so libinput sees finite velocities
        # instead of a zero-duration burst (capped at ~20 ms total).
        pace = min(0.002, 0.02 / max(len(self.relay_buf), 1))
        for i, frame in enumerate(self.relay_buf):
            evs = [(t, c, v) for t, c, v in frame if t != ec.EV_SYN]
            if evs:
                self._emit(evs)
                if i + 1 < len(self.relay_buf):
                    time.sleep(pace)
        self.relay_buf = []
        self.relay_pre = set()

    def process_frame(self, events):
        pre_active = set(self.down)
        self._apply(events)
        count = len(self.down)

        if self.mode == FORWARD:
            if count >= 3:
                self._relay_begin(pre_active, events)
            else:
                self._forward(events)
        elif self.mode == GESTURE:
            self.max_count = max(self.max_count, count)
            if self.sub == "move":
                if count == 0:
                    self._move_end()
                    self._reset_gesture()
                    self.mode = FORWARD
                else:
                    self._track()
            elif self.sub == "pass":
                self._forward(events)
                if count == 0:
                    self._reset_gesture()
                    self.mode = FORWARD
            elif count >= 3:
                if self.sub == "pending":
                    self.relay_buf.append(list(events))
                self._track()
            else:
                if self.sub == "pending":
                    self.relay_buf.append(list(events))
                    self._finalize()  # _check_taps claims or replays
                else:
                    self._finalize()
                if self.relay_claimed:
                    self.relay_claimed = False
                    self.mode = DRAIN if count > 0 else FORWARD
                else:
                    self.mode = FORWARD
        elif self.mode == DRAIN:
            if count == 0:
                self.mode = FORWARD

    # -- classification overrides ------------------------------------------

    def _engage_swipe(self, g):
        # 4-finger horizontal media swipes are ours; everything else is a
        # native niri gesture: replay and step aside.
        if len(self.down) == 4 and abs(self.acc[0]) > abs(self.acc[1]) \
                and self.pinch_action is not None:
            swipe4 = (self.cfg.get("actions") or {}).get("swipe4") or {}
            action = swipe4.get("left" if self.acc[0] < 0 else "right", "")
            if action:
                self._relay_cancel()
                self.sub = "done"
                log.info("trackpad 4-finger swipe -> %s", action)
                self.pinch_action(action, None)
                return
        self._relay_replay()
        self.sub = "pass"

    def _pinch_fire(self, key):
        actions = (self.cfg.get("actions") or {}).get("pinch") or {}
        action = actions.get(key, "")
        self._relay_cancel()
        self.sub = "done"
        if not action or self.pinch_action is None:
            return
        log.info("trackpad pinch %s -> %s", key, action)
        self.pinch_action(action, None)

    def _check_taps(self, g):
        """At gesture end: claim a 4/5-finger tap, else replay so native
        libinput taps (e.g. 3-finger middle click) keep working."""
        claimed = False
        now = time.monotonic()
        quick = (now - self.t0) * 1000.0 <= float(g.get("tap_ms", 250) or 250)
        still = math.hypot(*self.acc) < g["deadzone_mm"] and \
            abs(self.pinch_acc) < 5.0
        if quick and still and self.pinch_action is not None:
            taps = (self.cfg.get("actions") or {}).get("tap") or {}
            if self.max_count >= 5 and taps.get("five"):
                claimed = True
                log.info("trackpad 5-finger tap -> %s", taps["five"])
            elif self.max_count == 4 and taps.get("four"):
                claimed = True
                log.info("trackpad 4-finger tap -> %s", taps["four"])
        if claimed:
            action = taps["five"] if self.max_count >= 5 else taps["four"]
            self._relay_cancel()
            self.pinch_action(action, None)
        else:
            self._relay_replay()

    # -- hold-to-move at the cursor (relative mouse) -----------------------

    def _move_begin(self, button=ec.BTN_LEFT):
        if self.emit_rel is None or self.emit_kbd is None:
            return
        self._relay_cancel()
        self.sub = "move"
        self.move_btn = button
        self.fingers = len(self.down)
        self.last_acc = (self.acc[0], self.acc[1])
        self.rel_rem = [0.0, 0.0]
        self.emit_kbd([(ec.EV_KEY, self.move_key, 1)])
        time.sleep(0.04)
        self.emit_rel([(ec.EV_KEY, button, 1)])
        log.info("trackpad window %s begin",
                 "move" if button == ec.BTN_LEFT else "resize")

    def _move_update(self):
        dx = self.acc[0] - self.last_acc[0]
        dy = self.acc[1] - self.last_acc[1]
        if not dx and not dy:
            return
        self.last_acc = (self.acc[0], self.acc[1])
        # finger mm -> libinput-normalized units (1000 dpi equivalent)
        self.rel_rem[0] += dx * LIBINPUT_UNITS_PER_INCH_MM * self.move_gain
        self.rel_rem[1] += dy * LIBINPUT_UNITS_PER_INCH_MM * self.move_gain
        ix, iy = int(self.rel_rem[0]), int(self.rel_rem[1])
        if ix or iy:
            self.rel_rem[0] -= ix
            self.rel_rem[1] -= iy
            out = []
            if ix:
                out.append((ec.EV_REL, ec.REL_X, ix))
            if iy:
                out.append((ec.EV_REL, ec.REL_Y, iy))
            self.emit_rel(out)

    def _move_end(self):
        self.emit_rel([(ec.EV_KEY, self.move_btn, 0)])
        time.sleep(0.02)
        self.emit_kbd([(ec.EV_KEY, self.move_key, 0)])
        log.info("trackpad window move/resize end")

    def tick(self):
        if self.mode == GESTURE and self.sub == "pending":
            self._check_hold(self.cfg["gestures"])


# --------------------------------------------------------------------------
# device layer

def is_touchscreen(dev):
    try:
        props = dev.input_props()
        caps = dev.capabilities()
    except OSError:
        return False
    if ec.INPUT_PROP_DIRECT not in props:
        return False
    abs_codes = {c for c, _ in caps.get(ec.EV_ABS, [])}
    return ec.ABS_MT_SLOT in abs_codes and ec.ABS_MT_POSITION_X in abs_codes


def is_touchpad(dev):
    try:
        props = dev.input_props()
        caps = dev.capabilities()
    except OSError:
        return False
    if ec.INPUT_PROP_POINTER not in props:
        return False
    abs_codes = {c for c, _ in caps.get(ec.EV_ABS, [])}
    return ec.ABS_MT_SLOT in abs_codes and ec.ABS_MT_POSITION_X in abs_codes


def find_trackpad(cfg):
    want = cfg.get("trackpad", {}).get("name", "")
    for path in sorted(evdev.list_devices()):
        try:
            dev = InputDevice(path)
        except OSError:
            continue
        if VIRTUAL_NAME_SUFFIX in dev.name:
            dev.close()
            continue
        if want:
            if want in dev.name:
                return dev
        elif is_touchpad(dev):
            return dev
        dev.close()
    return None


def find_device(cfg):
    want = cfg["device"]["name"]
    for path in sorted(evdev.list_devices()):
        try:
            dev = InputDevice(path)
        except OSError:
            continue
        if VIRTUAL_NAME_SUFFIX in dev.name:
            dev.close()
            continue
        if want:
            if want in dev.name:
                return dev
        elif is_touchscreen(dev):
            return dev
        dev.close()
    return None


def axis_resolution(dev, axis, fallback_mm):
    info = dev.absinfo(axis)
    if info.resolution and info.resolution > 0:
        return float(info.resolution)  # units per mm
    span = max(info.max - info.min, 1)
    log.warning("no resolution reported for axis %d; assuming %.0f mm span",
                axis, fallback_mm)
    return span / fallback_mm


def niri_json(what):
    out = subprocess.run(["niri", "msg", "-j", what],
                         capture_output=True, timeout=2, env=niri_env())
    return json.loads(out.stdout)


def window_at(nx, ny):
    """Find the window whose tile contains the given normalized logical
    coordinates on the focused output. Returns its id, or None."""
    try:
        wss = niri_json("workspaces")
        ws = next((w for w in wss if w.get("is_focused")), None)
        if ws is None:
            return None
        outs = niri_json("outputs")
        out = outs.get(ws.get("output")) or next(iter(outs.values()), None)
        logical = (out or {}).get("logical") or {}
        lw, lh = logical.get("width"), logical.get("height")
        if not lw or not lh:
            return None
        px, py = nx * lw, ny * lh
        best = None
        for w in niri_json("windows"):
            if w.get("workspace_id") != ws.get("id"):
                continue
            lay = w.get("layout") or {}
            pos = lay.get("tile_pos_in_workspace_view")
            size = lay.get("tile_size")
            if not pos or not size:
                continue
            if pos[0] <= px < pos[0] + size[0] and \
                    pos[1] <= py < pos[1] + size[1]:
                # floating windows render above tiles; prefer them
                if best is None or \
                        (w.get("is_floating") and not best.get("is_floating")):
                    best = w
        return best.get("id") if best else None
    except Exception as exc:  # noqa: BLE001 - never kill the daemon here
        log.debug("window_at failed: %s", exc)
        return None


NIRI_GAPS = parse_niri_gaps()


def run_niri_action(argv):
    try:
        subprocess.Popen(["niri", "msg", "action"] + argv, env=niri_env(),
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except OSError as exc:
        log.error("niri action %r failed: %s", argv, exc)


def window_zoom(zoom_in):
    """Walk the focused window one stage along: partial column <->
    maximized column <-> maximized to edges <-> fullscreen."""
    try:
        wins = niri_json("windows")
        w = next((x for x in wins if x.get("is_focused")), None)
        if w is None:
            return
        outs = niri_json("outputs")
        logical = next((o.get("logical") for o in outs.values()
                        if o.get("logical")), None) or {}
        out_w, out_h = logical.get("width"), logical.get("height")
        if not out_w or not out_h:
            return
        tw, th = w["layout"]["tile_size"]
        if w.get("is_floating"):
            full = tw >= out_w - 2 and th >= out_h - 2
            if zoom_in != full:
                run_niri_action(["fullscreen-window", "--id", str(w["id"])])
            return
        stage = zoom_stage(tw, th, out_w, out_h, NIRI_GAPS)
        argv = zoom_action(zoom_in, stage, w["id"])
        log.info("window zoom %s: stage %d -> %s",
                 "in" if zoom_in else "out", stage, argv)
        if argv:
            run_niri_action(argv)
    except Exception as exc:  # noqa: BLE001
        log.error("window zoom failed: %s", exc)


def pinch_action(action, wid):
    """Run a niri action for a pinch gesture. close-window targets the
    window under the fingers by id; window-zoom-in/out are the staged size
    built-ins; spawn runs directly; anything else focuses the window under
    the fingers first."""
    action = action.strip()
    try:
        if action == "window-zoom-in":
            window_zoom(True)
            return
        if action == "window-zoom-out":
            window_zoom(False)
            return
        words = shlex.split(action)
        if words and words[0] == "spawn":
            run_niri_action(["spawn", "--"] + words[1:])
            return
        if words and words[0] in ("screenshot", "screenshot-screen",
                                  "screenshot-window"):
            run_niri_action(words)  # no window focusing needed
            return
        if action == "close-window":
            args = ["close-window"]
            if wid is not None:
                args += ["--id", str(wid)]
            run_niri_action(args)
            return
        if wid is not None:
            subprocess.run(
                ["niri", "msg", "action", "focus-window", "--id", str(wid)],
                env=niri_env(), timeout=2,
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        run_niri_action(words)
    except (OSError, subprocess.TimeoutExpired) as exc:
        log.error("pinch action %r failed: %s", action, exc)


def parse_level(text):
    """Extract a 0-100 level from command output: '37%' or a 0..1 fraction
    like wpctl's 'Volume: 0.45'."""
    m = re.search(r"(\d+(?:\.\d+)?)\s*%", text)
    if m:
        return float(m.group(1))
    m = re.search(r"\d+(?:\.\d+)?", text)
    if m:
        v = float(m.group(0))
        return v * 100.0 if v <= 1.5 else v
    return None


def move_edge_action(direction):
    """Workspace switch while dragging a window to the top/bottom edge.
    Skipped in the Overview, where niri's own edge switching handles it."""
    try:
        out = subprocess.run(["niri", "msg", "overview-state"],
                             capture_output=True, timeout=2, env=niri_env())
        if b"open" in out.stdout.lower():
            return
    except (OSError, subprocess.TimeoutExpired):
        pass
    run_niri_action(["focus-workspace-up" if direction == "up"
                     else "focus-workspace-down"])


class ActionRunner:
    def __init__(self, cfg, dry_run):
        self.cfg = cfg
        self.dry_run = dry_run
        self.base = shlex.split(cfg["actions"]["command"])
        self.children = []

    def __call__(self, fingers, direction):
        table = self.cfg["actions"]["three" if fingers == 3 else "four"]
        action = table.get(direction, "")
        if not action:
            log.debug("no action for %d-finger %s", fingers, direction)
            return
        log.info("%d-finger %s -> %s", fingers, direction, action)
        if self.dry_run:
            return
        self.children = [p for p in self.children if p.poll() is None]
        try:
            self.children.append(subprocess.Popen(
                self.base + shlex.split(action),
                env=niri_env(),
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            ))
        except OSError as exc:
            log.error("failed to run action %r: %s", action, exc)


def create_shared(cfg, dry_run):
    """Virtual devices shared by all gesture engines: an absolute pointer
    (touchscreen holds), a relative mouse (trackpad holds and the
    focus-under-cursor click), and a modifier keyboard."""
    shared = {"virtuals": [], "emit_ptr": None, "emit_kbd": None,
              "emit_rel": None, "trackpad_pinch": None,
              "focus_under_cursor": None}
    if dry_run or float(cfg["gestures"].get("hold_ms", 0) or 0) <= 0:
        return shared

    ptr = UInput(
        {
            ec.EV_ABS: [
                (ec.ABS_X, AbsInfo(0, 0, PTR_RANGE, 0, 0, 0)),
                (ec.ABS_Y, AbsInfo(0, 0, PTR_RANGE, 0, 0, 0)),
            ],
            ec.EV_KEY: [ec.BTN_LEFT, ec.BTN_RIGHT, ec.BTN_MIDDLE],
            ec.EV_REL: [ec.REL_WHEEL],
        },
        name=f"{VIRTUAL_NAME_SUFFIX} pointer",
    )
    kbd = UInput(
        {ec.EV_KEY: list(MODIFIER_KEYS.values())},
        name=f"{VIRTUAL_NAME_SUFFIX} keyboard",
    )
    rel = UInput(
        {
            ec.EV_REL: [ec.REL_X, ec.REL_Y],
            ec.EV_KEY: [ec.BTN_LEFT, ec.BTN_RIGHT],
        },
        name=f"{VIRTUAL_NAME_SUFFIX} mouse",
    )
    shared["virtuals"] = [ptr, kbd, rel]
    log.info("virtual pointer, mouse, and modifier keyboard created")

    def make_emit(device):
        def emit(events):
            for etype, code, value in events:
                device.write(etype, code, value)
            device.syn()
        return emit

    emit_ptr = make_emit(ptr)
    emit_kbd = make_emit(kbd)
    emit_rel = make_emit(rel)
    shared["emit_ptr"] = emit_ptr
    shared["emit_kbd"] = emit_kbd
    shared["emit_rel"] = emit_rel

    move_key = MODIFIER_KEYS.get(
        str(cfg["gestures"].get("move_modifier", "Super")).lower(),
        ec.KEY_LEFTMETA)

    def focus_under_cursor():
        """Focus the window under the pointer: a Mod+click is consumed by
        the compositor (zero-length move grab) and activates the window
        under the cursor, while apps never see the click."""
        emit_kbd([(ec.EV_KEY, move_key, 1)])
        time.sleep(0.04)
        emit_rel([(ec.EV_KEY, ec.BTN_LEFT, 1)])
        time.sleep(0.03)
        emit_rel([(ec.EV_KEY, ec.BTN_LEFT, 0)])
        time.sleep(0.02)
        emit_kbd([(ec.EV_KEY, move_key, 0)])
        time.sleep(0.05)

    def trackpad_pinch(action, _wid):
        if action.strip() == "close-window":
            focus_under_cursor()
            run_niri_action(["close-window"])
            return
        pinch_action(action, None)

    shared["focus_under_cursor"] = focus_under_cursor
    shared["trackpad_pinch"] = trackpad_pinch
    return shared


def make_clone(dev, dry_run):
    if dry_run:
        return None, (lambda events: None)
    ui = UInput.from_device(
        dev,
        name=f"{dev.name} {VIRTUAL_NAME_SUFFIX}",
        input_props=dev.input_props(),
    )
    log.info("virtual device created: %s", ui.name)

    def emit(events):
        for etype, code, value in events:
            ui.write(etype, code, value)
        ui.syn()

    return ui, emit


def make_touchscreen_runner(dev, cfg, dry_run, shared):
    log.info("touchscreen: %s (%s)", dev.name, dev.path)
    mm_x = axis_resolution(dev, ec.ABS_MT_POSITION_X,
                           cfg["device"]["fallback_width_mm"])
    mm_y = axis_resolution(dev, ec.ABS_MT_POSITION_Y,
                           cfg["device"]["fallback_height_mm"])
    log.info("resolution: %.1f x %.1f units/mm", mm_x, mm_y)

    ui, emit = make_clone(dev, dry_run)
    virtuals = [v for v in (ui,) if v is not None]

    # In native mode, gestures are replayed on a virtual touchpad so that
    # libinput + niri handle them with the exact same continuous physics as
    # the real trackpad. Sized 2x the screen, with contacts recentered, so
    # they never start in libinput's edge palm-detection zones nor run off.
    pad = None
    emit_pad = None
    pad_geom = None
    calib = None
    if cfg["gestures"].get("mode", "native") == "native" and not dry_run:
        xi = dev.absinfo(ec.ABS_MT_POSITION_X)
        yi = dev.absinfo(ec.ABS_MT_POSITION_Y)
        w_mm = (xi.max - xi.min + 1) / mm_x
        h_mm = (yi.max - yi.min + 1) / mm_y
        profile, speed = parse_niri_touchpad_accel()
        units = libinput_units_per_mm(profile, speed)
        calib = {"u": units, "w_mm": w_mm, "h_mm": h_mm}
        log.info("libinput touchpad accel: %s profile, speed %.2f "
                 "-> %.2f units/mm", profile, speed, units)
        log.info("auto 1:1 gains: x %.3f (view), y %.3f (workspace)",
                 NIRI_VIEW_MOVEMENT / (units * w_mm),
                 NIRI_WORKSPACE_MOVEMENT / (units * h_mm))
        pad_xmax = 2 * (xi.max - xi.min + 1) - 1
        pad_ymax = 2 * (yi.max - yi.min + 1) - 1
        res_x = max(1, int(round(mm_x)))
        res_y = max(1, int(round(mm_y)))
        caps = {
            ec.EV_KEY: [ec.BTN_LEFT, ec.BTN_TOUCH, ec.BTN_TOOL_FINGER,
                        ec.BTN_TOOL_DOUBLETAP, ec.BTN_TOOL_TRIPLETAP,
                        ec.BTN_TOOL_QUADTAP, ec.BTN_TOOL_QUINTTAP],
            ec.EV_ABS: [
                (ec.ABS_MT_SLOT, AbsInfo(0, 0, 5, 0, 0, 0)),
                (ec.ABS_MT_TRACKING_ID, AbsInfo(0, 0, 65535, 0, 0, 0)),
                (ec.ABS_MT_POSITION_X, AbsInfo(0, 0, pad_xmax, 0, 0, res_x)),
                (ec.ABS_MT_POSITION_Y, AbsInfo(0, 0, pad_ymax, 0, 0, res_y)),
                (ec.ABS_X, AbsInfo(0, 0, pad_xmax, 0, 0, res_x)),
                (ec.ABS_Y, AbsInfo(0, 0, pad_ymax, 0, 0, res_y)),
            ],
        }
        pad = UInput(
            caps, name=f"{VIRTUAL_NAME_SUFFIX} touchpad",
            input_props=[ec.INPUT_PROP_POINTER, ec.INPUT_PROP_BUTTONPAD],
        )
        virtuals.append(pad)
        pad_geom = {"cx": pad_xmax // 2, "cy": pad_ymax // 2,
                    "xmax": pad_xmax, "ymax": pad_ymax}
        log.info("virtual touchpad created (%.0f x %.0f mm)",
                 (pad_xmax + 1) / mm_x, (pad_ymax + 1) / mm_y)

        def emit_pad(events):
            for etype, code, value in events:
                pad.write(etype, code, value)
            pad.syn()

    def strip_cfg(key):
        return ((cfg.get("actions") or {}).get("strip") or {}).get(key, "")

    def strip_get(side):
        cmd = strip_cfg(f"{side}_get")
        if not cmd:
            return None
        try:
            out = subprocess.run(shlex.split(cmd), capture_output=True,
                                 text=True, timeout=1)
            return parse_level(out.stdout)
        except (OSError, subprocess.TimeoutExpired) as exc:
            log.warning("strip get %r failed: %s", cmd, exc)
            return None

    def strip_set(side, value):
        cmd = strip_cfg(f"{side}_set")
        if not cmd:
            return
        argv = [a.replace("{}", str(int(value))) for a in shlex.split(cmd)]
        try:
            subprocess.Popen(argv, stdout=subprocess.DEVNULL,
                             stderr=subprocess.DEVNULL)
        except OSError as exc:
            log.error("strip set %r failed: %s", cmd, exc)

    has_hold = shared["emit_ptr"] is not None
    engine = GestureEngine(
        cfg, emit, ActionRunner(cfg, dry_run), mm_x, mm_y,
        TransformCache(cfg["device"]["transform"]).get,
        emit_pad=emit_pad, pad_geom=pad_geom, calib=calib,
        emit_ptr=shared["emit_ptr"], emit_kbd=shared["emit_kbd"],
        window_at=window_at if pad is not None else None,
        pinch_action=pinch_action if pad is not None else None,
        move_edge=move_edge_action if has_hold else None,
        strip_get=strip_get if pad is not None else None,
        strip_set=strip_set if pad is not None else None,
    )

    dev.grab()
    os.set_blocking(dev.fd, False)
    log.info("grabbed %s", dev.path)
    return {"dev": dev, "engine": engine, "frame": [], "virtuals": virtuals}


def make_trackpad_runner(dev, cfg, dry_run, shared):
    log.info("trackpad: %s (%s)", dev.name, dev.path)
    mm_x = axis_resolution(dev, ec.ABS_MT_POSITION_X, 100.0)
    mm_y = axis_resolution(dev, ec.ABS_MT_POSITION_Y, 60.0)
    ui, emit = make_clone(dev, dry_run)
    xi = dev.absinfo(ec.ABS_MT_POSITION_X)
    yi = dev.absinfo(ec.ABS_MT_POSITION_Y)
    calib = {"u": libinput_units_per_mm("adaptive", 0.0),
             "w_mm": (xi.max - xi.min + 1) / mm_x,
             "h_mm": (yi.max - yi.min + 1) / mm_y}
    tp = cfg["trackpad"]
    tcfg = deep_merge(cfg, {"gestures": {
        "deadzone_mm": float(tp.get("deadzone_mm", 3.0) or 3.0),
        "pinch_close_mm": float(tp.get("pinch_close_mm", 6.0) or 0),
        "pinch_out_mm": float(tp.get("pinch_out_mm", 6.0) or 0),
    }})
    engine = TrackpadEngine(
        tcfg, emit, mm_x, mm_y,
        emit_kbd=shared["emit_kbd"], emit_rel=shared["emit_rel"],
        pinch_action=shared["trackpad_pinch"],
        calib=calib,
        move_gain=float(cfg["trackpad"].get("move_gain", 1.0) or 1.0),
    )
    dev.grab()
    os.set_blocking(dev.fd, False)
    log.info("grabbed %s", dev.path)
    return {"dev": dev, "engine": engine, "frame": [],
            "virtuals": [v for v in (ui,) if v is not None]}


def close_runner(runner):
    try:
        runner["dev"].ungrab()
    except OSError:
        pass
    runner["dev"].close()
    for v in runner["virtuals"]:
        v.close()


def main_loop(cfg, dry_run):
    shared = create_shared(cfg, dry_run)
    runners = {}
    want_tp = bool(cfg.get("trackpad", {}).get("enabled", True))
    missing_logged = set()
    last_scan = 0.0
    try:
        while True:
            now = time.monotonic()
            need_ts = "ts" not in runners
            need_tp = want_tp and "tp" not in runners
            if (need_ts or need_tp) and now - last_scan >= 2.0:
                last_scan = now
                if need_ts:
                    dev = find_device(cfg)
                    if dev is not None:
                        runners["ts"] = make_touchscreen_runner(
                            dev, cfg, dry_run, shared)
                        missing_logged.discard("ts")
                    elif "ts" not in missing_logged:
                        log.info("no readable touchscreen found, waiting")
                        missing_logged.add("ts")
                if need_tp:
                    dev = find_trackpad(cfg)
                    if dev is not None:
                        runners["tp"] = make_trackpad_runner(
                            dev, cfg, dry_run, shared)
                        missing_logged.discard("tp")
                    elif "tp" not in missing_logged:
                        log.info("no readable trackpad found, waiting "
                                 "(udev rule needed?)")
                        missing_logged.add("tp")
            if not runners:
                time.sleep(2)
                continue
            fd_map = {r["dev"].fd: (key, r) for key, r in runners.items()}
            ready, _, _ = select.select(list(fd_map), [], [], 0.05)
            for fd in ready:
                key, runner = fd_map[fd]
                try:
                    events = list(runner["dev"].read())
                except BlockingIOError:
                    continue
                except OSError as exc:
                    log.warning("%s device error (%s); rescanning", key, exc)
                    close_runner(runner)
                    del runners[key]
                    continue
                for event in events:
                    if event.type == ec.EV_SYN:
                        if event.code == ec.SYN_REPORT:
                            runner["engine"].process_frame(runner["frame"])
                            runner["frame"] = []
                        elif event.code == ec.SYN_DROPPED:
                            log.warning("SYN_DROPPED on %s", key)
                            runner["frame"] = []
                    else:
                        runner["frame"].append(
                            (event.type, event.code, event.value))
            for runner in runners.values():
                runner["engine"].tick()
    finally:
        for runner in runners.values():
            close_runner(runner)
        for v in shared["virtuals"]:
            v.close()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--config",
                        default=os.path.expanduser(
                            "~/.config/niri-touch-gestures/config.toml"))
    parser.add_argument("--device", help="override device name match")
    parser.add_argument("--dry-run", action="store_true",
                        help="no virtual device, no actions; log only")
    parser.add_argument("--list-devices", action="store_true")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(levelname)s %(message)s",
    )

    if args.list_devices:
        for path in sorted(evdev.list_devices()):
            try:
                dev = InputDevice(path)
            except OSError as exc:
                print(f"{path}: not readable ({exc})")
                continue
            kind = "touchscreen" if is_touchscreen(dev) else \
                "touchpad" if is_touchpad(dev) else ""
            print(f"{path}  {dev.name!r}  {kind}")
            dev.close()
        return 0

    cfg = load_config(args.config)
    if args.device:
        cfg["device"]["name"] = args.device

    def _sig(signum, _frame):
        raise SystemExit(0)

    signal.signal(signal.SIGTERM, _sig)
    signal.signal(signal.SIGINT, _sig)

    try:
        main_loop(cfg, args.dry_run)
    except SystemExit:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
