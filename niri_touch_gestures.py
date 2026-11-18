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
from evdev import InputDevice, UInput, ecodes as ec

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
        # Travel before a swipe's axis is decided (mm).
        "deadzone_mm": 6.0,
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

    def __init__(self, cfg, emit, act, mm_x, mm_y, transform_getter):
        self.cfg = cfg
        self.emit = emit
        self.act = act
        self.mm_x = mm_x  # units per mm
        self.mm_y = mm_y
        self.get_transform = transform_getter

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
            if count >= 3:
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
        self.transform = TRANSFORMS.get(self.get_transform(),
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
        if self.axis is not None and not self.fired and len(self.samples) >= 2:
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
        log.info("virtual device created: %s", ui.device.path if ui.device else "?")

    def emit(events):
        if ui is None:
            return
        for etype, code, value in events:
            ui.write(etype, code, value)
        ui.syn()

    engine = GestureEngine(
        cfg, emit, ActionRunner(cfg, dry_run), mm_x, mm_y,
        TransformCache(cfg["device"]["transform"]).get,
    )

    dev.grab()
    log.info("grabbed %s", dev.path)
    frame = []
    try:
        for event in dev.read_loop():
            if event.type == ec.EV_SYN:
                if event.code == ec.SYN_REPORT:
                    engine.process_frame(frame)
                    frame = []
                elif event.code == ec.SYN_DROPPED:
                    log.warning("SYN_DROPPED: kernel event buffer overrun")
                    frame = []
            else:
                frame.append((event.type, event.code, event.value))
    finally:
        try:
            dev.ungrab()
        except OSError:
            pass
        dev.close()
        if ui is not None:
            ui.close()


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
