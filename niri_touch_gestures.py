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
        # Hold 3 fingers still for this long (ms) to pick up the window
        # under them and move it, exactly like Mod+drag with the mouse.
        # 0 disables. Native mode only.
        "hold_ms": 350,
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
        "pinch": {
            "in3": "close-window",
            "out3": "maximize-column",
            "in4": "",
            "out4": "maximize-window-to-edges",
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


class GestureEngine:
    """Consumes evdev frames, forwards or interprets them.

    emit(events): forward a list of (type, code, value) to the virtual device
                  (the caller appends SYN_REPORT).
    act(fingers, direction): fingers is 3 or 4; direction in l/r/u/d words.
    """

    def __init__(self, cfg, emit, act, mm_x, mm_y, transform_getter,
                 emit_pad=None, pad_geom=None, calib=None,
                 emit_ptr=None, emit_kbd=None,
                 window_at=None, pinch_action=None):
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
            else:
                self._forward(events)
        elif self.mode == GESTURE:
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

        travel = math.hypot(*self.acc)
        n = len(self.down)
        pinch_in = float(g.get("pinch_close_mm", 0) or 0)
        pinch_out = float(g.get("pinch_out_mm", 0) or 0)

        if n in (3, 4):
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
        if travel >= g["deadzone_mm"] and travel >= 1.2 * abs(self.pinch_acc):
            self._engage_swipe(g)
            return
        self._check_hold(g)

    def _engage_swipe(self, g):
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
        if len(self.down) != 3:
            return
        if (time.monotonic() - self.t0) * 1000.0 < hold_ms:
            return
        if math.hypot(*self.acc) >= g["deadzone_mm"] or \
                abs(self.pinch_acc) >= 5.0:
            return
        self._move_begin()

    def tick(self):
        """Called periodically by the event loop; triggers time-based
        gestures (hold) even when perfectly still fingers send no events."""
        if self.mode == GESTURE and self.sub == "pending":
            self._check_hold(self.cfg["gestures"])

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

    def _move_begin(self):
        pos = self._centroid_norm_logical()
        if pos is None:
            return
        self.sub = "move"
        self.fingers = len(self.down)
        self.ptr_pos = [pos[0] * PTR_RANGE, pos[1] * PTR_RANGE]
        self.last_acc = (self.acc[0], self.acc[1])
        self.emit_ptr([(ec.EV_ABS, ec.ABS_X, int(round(self.ptr_pos[0]))),
                       (ec.EV_ABS, ec.ABS_Y, int(round(self.ptr_pos[1])))])
        self.emit_kbd([(ec.EV_KEY, self.move_key, 1)])
        self.emit_ptr([(ec.EV_KEY, ec.BTN_LEFT, 1)])
        log.info("window move begin")

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

    def _move_end(self):
        self.emit_ptr([(ec.EV_KEY, ec.BTN_LEFT, 0)])
        self.emit_kbd([(ec.EV_KEY, self.move_key, 0)])
        log.info("window move end")

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

    def _finalize(self):
        g = self.cfg["gestures"]
        if self.engaged:
            self._release_pad()
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


def pinch_action(action, wid):
    """Run a niri action on the window under the fingers: close-window
    targets it directly by id; anything else focuses it first."""
    env = niri_env()
    try:
        if action.strip() == "close-window":
            args = ["niri", "msg", "action", "close-window"]
            if wid is not None:
                args += ["--id", str(wid)]
            subprocess.Popen(args, env=env, stdout=subprocess.DEVNULL,
                             stderr=subprocess.DEVNULL)
            return
        if wid is not None:
            subprocess.run(
                ["niri", "msg", "action", "focus-window", "--id", str(wid)],
                env=env, timeout=2,
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        subprocess.Popen(
            ["niri", "msg", "action"] + shlex.split(action),
            env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except (OSError, subprocess.TimeoutExpired) as exc:
        log.error("pinch action %r failed: %s", action, exc)


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


def run_device(dev, cfg, dry_run):
    log.info("using %s (%s)", dev.name, dev.path)
    mm_x = axis_resolution(dev, ec.ABS_MT_POSITION_X,
                           cfg["device"]["fallback_width_mm"])
    mm_y = axis_resolution(dev, ec.ABS_MT_POSITION_Y,
                           cfg["device"]["fallback_height_mm"])
    log.info("resolution: %.1f x %.1f units/mm", mm_x, mm_y)

    ui = None
    if not dry_run:
        ui = UInput.from_device(
            dev,
            name=f"{dev.name} {VIRTUAL_NAME_SUFFIX}",
            input_props=dev.input_props(),
        )
        log.info("virtual device created: %s", ui.name)

    def emit(events):
        if ui is None:
            return
        for etype, code, value in events:
            ui.write(etype, code, value)
        ui.syn()

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
        pad_geom = {"cx": pad_xmax // 2, "cy": pad_ymax // 2,
                    "xmax": pad_xmax, "ymax": pad_ymax}
        log.info("virtual touchpad created (%.0f x %.0f mm)",
                 (pad_xmax + 1) / mm_x, (pad_ymax + 1) / mm_y)

        def emit_pad(events):
            for etype, code, value in events:
                pad.write(etype, code, value)
            pad.syn()

    # Window gestures: a virtual absolute pointer + modifier keyboard let a
    # 3-finger hold perform a genuine Mod+drag interactive window move.
    ptr = None
    kbd = None
    emit_ptr = None
    emit_kbd = None
    if pad is not None and float(cfg["gestures"].get("hold_ms", 0) or 0) > 0:
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
        log.info("virtual pointer and modifier keyboard created")

        def emit_ptr(events):
            for etype, code, value in events:
                ptr.write(etype, code, value)
            ptr.syn()

        def emit_kbd(events):
            for etype, code, value in events:
                kbd.write(etype, code, value)
            kbd.syn()

    engine = GestureEngine(
        cfg, emit, ActionRunner(cfg, dry_run), mm_x, mm_y,
        TransformCache(cfg["device"]["transform"]).get,
        emit_pad=emit_pad, pad_geom=pad_geom, calib=calib,
        emit_ptr=emit_ptr, emit_kbd=emit_kbd,
        window_at=window_at if pad is not None else None,
        pinch_action=pinch_action if pad is not None else None,
    )

    dev.grab()
    log.info("grabbed %s", dev.path)
    os.set_blocking(dev.fd, False)
    frame = []
    try:
        while True:
            ready, _, _ = select.select([dev.fd], [], [], 0.05)
            if not ready:
                engine.tick()
                continue
            try:
                events = list(dev.read())
            except BlockingIOError:
                continue
            for event in events:
                if event.type == ec.EV_SYN:
                    if event.code == ec.SYN_REPORT:
                        engine.process_frame(frame)
                        frame = []
                    elif event.code == ec.SYN_DROPPED:
                        log.warning("SYN_DROPPED: kernel event buffer overrun")
                        frame = []
                else:
                    frame.append((event.type, event.code, event.value))
            engine.tick()
    finally:
        try:
            dev.ungrab()
        except OSError:
            pass
        dev.close()
        for virtual in (ui, pad, ptr, kbd):
            if virtual is not None:
                virtual.close()


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
            kind = "touchscreen" if is_touchscreen(dev) else ""
            print(f"{path}  {dev.name!r}  {kind}")
            dev.close()
        return 0

    cfg = load_config(args.config)
    if args.device:
        cfg["device"]["name"] = args.device

    stop = {"flag": False}

    def _sig(signum, _frame):
        stop["flag"] = True
        raise SystemExit(0)

    signal.signal(signal.SIGTERM, _sig)
    signal.signal(signal.SIGINT, _sig)

    while not stop["flag"]:
        dev = find_device(cfg)
        if dev is None:
            log.info("no touchscreen found, retrying in 2 s")
            time.sleep(2)
            continue
        try:
            run_device(dev, cfg, args.dry_run)
        except SystemExit:
            break
        except OSError as exc:
            log.warning("device error (%s); rescanning", exc)
            time.sleep(1)
    return 0


if __name__ == "__main__":
    sys.exit(main())
