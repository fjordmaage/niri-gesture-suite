#!/usr/bin/env python3
"""In-process tests for the gesture engine.

Simulates a protocol-B multitouch device (only changed values are reported,
like real hardware) and checks both gesture recognition and the passthrough /
cancel / cache-resync behaviour of the virtual clone.

Run:  python3 tests/test_engine.py
"""
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import niri_gesture_suite as ntg
from evdev import ecodes as ec

RES = 10.0  # units per mm


class FakeClock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


class FakeTouch:
    """Emits evdev frames like a real protocol-B device: diffs only."""

    def __init__(self):
        self.cur_slot = 0
        self.ids = {}
        self.cache = {}
        self.next_id = 1
        self.pending = []
        self.btn_touch = 0

    def _slot(self, slot):
        if self.cur_slot != slot:
            self.pending.append((ec.EV_ABS, ec.ABS_MT_SLOT, slot))
            self.cur_slot = slot

    def _pos(self, slot, x, y):
        cx, cy = self.cache.get(slot, (None, None))
        if x != cx:
            self._slot(slot)
            self.pending.append((ec.EV_ABS, ec.ABS_MT_POSITION_X, x))
        if y != cy:
            self._slot(slot)
            self.pending.append((ec.EV_ABS, ec.ABS_MT_POSITION_Y, y))
        self.cache[slot] = (x, y)

    def down(self, slot, x, y):
        self._slot(slot)
        self.pending.append((ec.EV_ABS, ec.ABS_MT_TRACKING_ID, self.next_id))
        self.ids[slot] = self.next_id
        self.next_id += 1
        self._pos(slot, x, y)
        if not self.btn_touch:
            self.pending.append((ec.EV_KEY, ec.BTN_TOUCH, 1))
            self.btn_touch = 1

    def move(self, slot, x, y):
        self._pos(slot, x, y)

    def up(self, slot):
        self._slot(slot)
        self.pending.append((ec.EV_ABS, ec.ABS_MT_TRACKING_ID, -1))
        del self.ids[slot]
        if not self.ids and self.btn_touch:
            self.pending.append((ec.EV_KEY, ec.BTN_TOUCH, 0))
            self.btn_touch = 0

    def frame(self):
        out, self.pending = self.pending, []
        return out


PAD_GEOM = {"cx": 2560, "cy": 1790, "xmax": 5119, "ymax": 3579}


def make_engine(clock, mode="discrete", scales=(1.0, 1.0, 1.0), calib=None):
    import copy
    cfg = copy.deepcopy(ntg.DEFAULT_CONFIG)
    cfg["gestures"]["mode"] = mode
    cfg["gestures"]["scale_x"] = scales[0]
    cfg["gestures"]["scale_y"] = scales[1]
    cfg["gestures"]["scale_overview"] = scales[2]
    emitted = []
    acts = []
    pad_events = []
    ptr_events = []
    kbd_events = []
    closed = []
    edge_moves = []
    strips = []
    native = mode == "native"
    engine = ntg.GestureEngine(
        cfg,
        emit=lambda evs: emitted.append(list(evs)),
        act=lambda n, d: acts.append((n, d)),
        mm_x=RES, mm_y=RES,
        transform_getter=lambda: "normal",
        emit_pad=(lambda evs: pad_events.append(list(evs))) if native else None,
        pad_geom=PAD_GEOM if native else None,
        calib=calib,
        emit_ptr=(lambda evs: ptr_events.append(list(evs))) if native else None,
        emit_kbd=(lambda evs: kbd_events.append(list(evs))) if native else None,
        window_at=(lambda nx, ny: 42) if native else None,
        pinch_action=(lambda a, wid: closed.append((a, wid)))
                     if native else None,
        move_edge=(lambda d: edge_moves.append(d)) if native else None,
        strip_get=(lambda s: 40.0) if native else None,
        strip_set=(lambda s, v: strips.append((s, v))) if native else None,
    )
    engine._pad_events = pad_events
    engine._ptr_events = ptr_events
    engine._kbd_events = kbd_events
    engine._closed = closed
    engine._edge_moves = edge_moves
    engine._strips = strips
    return engine, emitted, acts


def mm(v):
    return int(v * RES)


def clone_state(emitted):
    """Replay emitted frames into a model of what apps would see."""
    cur = 0
    down = {}
    pos = {}
    keys = {}
    for frame in emitted:
        for etype, code, value in frame:
            if etype == ec.EV_ABS:
                if code == ec.ABS_MT_SLOT:
                    cur = value
                elif code == ec.ABS_MT_TRACKING_ID:
                    if value == -1:
                        down.pop(cur, None)
                    else:
                        down[cur] = value
                elif code == ec.ABS_MT_POSITION_X:
                    pos.setdefault(cur, [None, None])[0] = value
                elif code == ec.ABS_MT_POSITION_Y:
                    pos.setdefault(cur, [None, None])[1] = value
            elif etype == ec.EV_KEY:
                keys[code] = value
    return down, pos, keys


def test_single_finger_passthrough():
    clock = FakeClock()
    engine, emitted, acts = make_engine(clock)
    dev = FakeTouch()

    dev.down(0, 1000, 1000)
    engine.process_frame(dev.frame())
    dev.move(0, 1050, 1000)
    engine.process_frame(dev.frame())
    dev.up(0)
    engine.process_frame(dev.frame())

    down, pos, keys = clone_state(emitted)
    assert not down, "finger should be lifted at the end"
    assert pos[0] == [1050, 1000], f"clone position wrong: {pos}"
    assert keys.get(ec.BTN_TOUCH) == 0
    assert acts == [], f"no actions expected: {acts}"
    print("ok  single finger passthrough")


def test_three_finger_swipe_right_and_cancel():
    clock = FakeClock()
    engine, emitted, acts = make_engine(clock)
    dev = FakeTouch()

    # fingers land staggered, like real hands
    dev.down(0, 1000, 1000)
    engine.process_frame(dev.frame())
    dev.down(1, 1200, 1000)
    engine.process_frame(dev.frame())
    n_before = len(emitted)
    dev.down(2, 1400, 1000)
    engine.process_frame(dev.frame())

    # the 2 forwarded fingers must have been cancelled for apps
    down, pos, keys = clone_state(emitted)
    assert not down, f"clone should have no fingers down, has {down}"
    assert keys.get(ec.BTN_TOUCH) == 0, "BTN_TOUCH should be cleared"
    assert len(emitted) == n_before + 1, "cancel frame expected"

    # swipe right 30 mm in small steps
    for i in range(1, 11):
        clock.t += 0.01
        for s in range(3):
            dev.move(s, 1000 + s * 200 + mm(3) * i, 1000)
        engine.process_frame(dev.frame())
    assert acts == [(3, "right")], f"expected one 3-right, got {acts}"

    # keep going another 36 mm -> second step
    for i in range(1, 13):
        clock.t += 0.01
        for s in range(3):
            dev.move(s, 1000 + s * 200 + mm(30) + mm(3) * i, 1000)
        engine.process_frame(dev.frame())
    assert acts == [(3, "right"), (3, "right")], f"got {acts}"

    # lift all; nothing more may be forwarded until then
    n_before = len(emitted)
    for s in range(3):
        dev.up(s)
    engine.process_frame(dev.frame())
    assert len(emitted) == n_before, "gesture frames must be swallowed"
    print("ok  3-finger swipe right fires repeat steps, cancels clone touches")


def test_three_finger_swipe_down():
    clock = FakeClock()
    engine, emitted, acts = make_engine(clock)
    dev = FakeTouch()
    for s in range(3):
        dev.down(s, 1000 + s * 200, 800)
    engine.process_frame(dev.frame())
    for i in range(1, 11):
        clock.t += 0.01
        for s in range(3):
            dev.move(s, 1000 + s * 200, 800 + mm(3) * i)
        engine.process_frame(dev.frame())
    for s in range(3):
        dev.up(s)
    engine.process_frame(dev.frame())
    assert acts == [(3, "down")], f"got {acts}"
    print("ok  3-finger swipe down")


def test_four_finger_up_then_reverse():
    clock = FakeClock()
    engine, emitted, acts = make_engine(clock)
    dev = FakeTouch()
    for s in range(4):
        dev.down(s, 800 + s * 200, 1500)
    engine.process_frame(dev.frame())
    # up 24 mm -> open-overview (overview_mm = 22)
    for i in range(1, 9):
        clock.t += 0.01
        for s in range(4):
            dev.move(s, 800 + s * 200, 1500 - mm(3) * i)
        engine.process_frame(dev.frame())
    assert acts == [(4, "up")], f"got {acts}"
    # reverse down 40 mm within the same gesture -> close-overview
    for i in range(1, 15):
        clock.t += 0.01
        for s in range(4):
            dev.move(s, 800 + s * 200, 1500 - mm(24) + mm(3) * i)
        engine.process_frame(dev.frame())
    assert acts == [(4, "up"), (4, "down")], f"got {acts}"
    print("ok  4-finger up opens, reverse closes, within one gesture")


def test_flick():
    clock = FakeClock()
    engine, emitted, acts = make_engine(clock)
    dev = FakeTouch()
    for s in range(3):
        dev.down(s, 1000 + s * 200, 1000)
    engine.process_frame(dev.frame())
    # 12 mm in 50 ms = 240 mm/s, above flick_speed 130 mm/s
    for i in range(1, 5):
        clock.t += 0.0125
        for s in range(3):
            dev.move(s, 1000 + s * 200 + mm(3) * i, 1000)
        engine.process_frame(dev.frame())
    for s in range(3):
        dev.up(s)
    engine.process_frame(dev.frame())
    assert acts == [(3, "right")], f"flick should fire once, got {acts}"
    print("ok  short fast flick fires one step")


def test_deadzone_no_fire():
    clock = FakeClock()
    engine, emitted, acts = make_engine(clock)
    dev = FakeTouch()
    for s in range(3):
        dev.down(s, 1000 + s * 200, 1000)
    engine.process_frame(dev.frame())
    clock.t += 0.3
    for s in range(3):
        dev.move(s, 1000 + s * 200 + mm(3), 1000)  # 3 mm < deadzone
    engine.process_frame(dev.frame())
    clock.t += 0.3
    for s in range(3):
        dev.up(s)
    engine.process_frame(dev.frame())
    assert acts == [], f"jitter below deadzone must not fire: {acts}"
    print("ok  sub-deadzone 3-finger touch does nothing")


def test_clone_cache_resync():
    """After a gesture, a new touch at coordinates the real device considers
    unchanged must still reach the clone correctly."""
    clock = FakeClock()
    engine, emitted, acts = make_engine(clock)
    dev = FakeTouch()

    # plain touch at (500, 500) -> clone cache now 500,500 for slot 0
    dev.down(0, 500, 500)
    engine.process_frame(dev.frame())
    dev.up(0)
    engine.process_frame(dev.frame())

    # gesture that drags slot 0 to (800, 800), swallowed from the clone
    dev.down(0, 500, 500)
    engine.process_frame(dev.frame())
    dev.down(1, 700, 500)
    engine.process_frame(dev.frame())
    dev.down(2, 900, 500)
    engine.process_frame(dev.frame())
    for i in range(1, 11):
        clock.t += 0.01
        dev.move(0, 500 + 30 * i, 500 + 30 * i)
        dev.move(1, 700 + 30 * i, 500 + 30 * i)
        dev.move(2, 900 + 30 * i, 500 + 30 * i)
        engine.process_frame(dev.frame())
    for s in range(3):
        dev.up(s)
    engine.process_frame(dev.frame())

    # new touch at exactly (800, 800): the fake device emits no X/Y
    # because its own cache already holds those values
    dev.down(0, 800, 800)
    frame = dev.frame()
    assert not any(c in (ec.ABS_MT_POSITION_X, ec.ABS_MT_POSITION_Y)
                   for _, c, _ in frame), "precondition: device sends no X/Y"
    engine.process_frame(frame)

    down, pos, keys = clone_state(emitted)
    assert 0 in down, "clone should see the finger down"
    assert pos[0] == [800, 800], f"clone must be resynced to 800,800: {pos}"
    print("ok  clone position cache resynced after swallowed gesture")


def test_native_mirror_lifecycle():
    clock = FakeClock()
    engine, emitted, acts = make_engine(clock, mode="native")
    pad = engine._pad_events
    dev = FakeTouch()

    dev.down(0, 1000, 1000)
    engine.process_frame(dev.frame())
    dev.down(1, 1200, 1000)
    engine.process_frame(dev.frame())
    dev.down(2, 1400, 1000)
    engine.process_frame(dev.frame())
    assert pad == [], "pad must stay quiet before the deadzone"

    # move right 10 mm -> mirror engages
    for i in range(1, 5):
        clock.t += 0.01
        for s in range(3):
            dev.move(s, 1000 + s * 200 + mm(2.5) * i, 1000)
        engine.process_frame(dev.frame())
    down, pos, keys = clone_state(pad)
    assert len(down) == 3, f"3 mirrored contacts expected: {down}"
    assert keys.get(ec.BTN_TOUCH) == 1
    assert keys.get(ec.BTN_TOOL_TRIPLETAP) == 1
    # mirrored contacts recenter at engage, then the pre-engage travel is
    # replayed, so the pad centroid reflects the full 10 mm from the start
    cx = sum(p[0] for p in pos.values() if p[0] is not None) / 3
    assert abs(cx - (PAD_GEOM["cx"] + mm(10))) <= 2, \
        f"centroid {cx} != center + 10 mm ({PAD_GEOM['cx'] + mm(10)})"

    # keep moving 20 mm -> pad centroid follows 1:1
    for i in range(1, 9):
        clock.t += 0.01
        for s in range(3):
            dev.move(s, 1000 + s * 200 + mm(10) + mm(2.5) * i, 1000)
        engine.process_frame(dev.frame())
    down, pos, keys = clone_state(pad)
    cx2 = sum(pos[s][0] for s in down) / 3
    assert abs((cx2 - cx) - mm(20)) <= 2, f"pad moved {cx2 - cx}, expected {mm(20)}"

    # release -> everything lifted cleanly
    for s in range(3):
        dev.up(s)
    engine.process_frame(dev.frame())
    down, pos, keys = clone_state(pad)
    assert not down, f"pad contacts must lift on release: {down}"
    assert keys.get(ec.BTN_TOUCH) == 0
    assert keys.get(ec.BTN_TOOL_TRIPLETAP) == 0
    assert acts == [], "native mode must not fire discrete actions"

    # clone (apps) saw only the cancel of the first two fingers, no gesture
    cdown, _, ckeys = clone_state(emitted)
    assert not cdown and ckeys.get(ec.BTN_TOUCH) == 0
    print("ok  native mode mirrors 1:1 onto virtual touchpad")


def test_native_below_deadzone_quiet():
    clock = FakeClock()
    engine, emitted, acts = make_engine(clock, mode="native")
    dev = FakeTouch()
    for s in range(3):
        dev.down(s, 1000 + s * 200, 1000)
    engine.process_frame(dev.frame())
    clock.t += 0.2
    for s in range(3):
        dev.move(s, 1000 + s * 200 + mm(3), 1000)
    engine.process_frame(dev.frame())
    for s in range(3):
        dev.up(s)
    engine.process_frame(dev.frame())
    assert engine._pad_events == [], "sub-deadzone rest must not touch the pad"
    print("ok  native mode: 3-finger rest never reaches the pad")


def test_native_finger_count_transition():
    clock = FakeClock()
    engine, emitted, acts = make_engine(clock, mode="native")
    pad = engine._pad_events
    dev = FakeTouch()
    for s in range(3):
        dev.down(s, 1000 + s * 200, 1500)
    engine.process_frame(dev.frame())
    for i in range(1, 5):
        clock.t += 0.01
        for s in range(3):
            dev.move(s, 1000 + s * 200, 1500 - mm(2.5) * i)
        engine.process_frame(dev.frame())
    down, pos, keys = clone_state(pad)
    assert keys.get(ec.BTN_TOOL_TRIPLETAP) == 1
    # 4th finger joins mid-gesture
    dev.down(3, 1600, 1500 - mm(10))
    engine.process_frame(dev.frame())
    down, pos, keys = clone_state(pad)
    assert len(down) == 4, f"4 contacts expected: {down}"
    assert keys.get(ec.BTN_TOOL_TRIPLETAP) == 0
    assert keys.get(ec.BTN_TOOL_QUADTAP) == 1
    for s in range(4):
        dev.up(s)
    engine.process_frame(dev.frame())
    down, pos, keys = clone_state(pad)
    assert not down and keys.get(ec.BTN_TOOL_QUADTAP) == 0
    print("ok  native mode: 3->4 finger transition updates BTN_TOOL_*")


def test_auto_gains():
    """auto scales follow niri + libinput constants: 1:1 content tracking."""
    clock = FakeClock()
    calib = {"u": 10.0, "w_mm": 240.0, "h_mm": 120.0}
    engine, emitted, acts = make_engine(
        clock, mode="native", scales=("auto", "auto", 2.0), calib=calib)
    pad = engine._pad_events
    dev = FakeTouch()
    # horizontal 3f: sx = 1200 / (10 * 240) = 0.5
    for s in range(3):
        dev.down(s, 1000 + s * 200, 1000)
    engine.process_frame(dev.frame())
    for i in range(1, 13):
        clock.t += 0.01
        for s in range(3):
            dev.move(s, 1000 + s * 200 + int(mm(2.5)) * i, 1000)
        engine.process_frame(dev.frame())
    down, pos, keys = clone_state(pad)
    cx = sum(pos[s][0] for s in down) / 3
    # 30 mm of finger travel * 0.5 = 15 mm of pad travel (replay included)
    assert abs(cx - (PAD_GEOM["cx"] + mm(15))) <= 3, \
        f"pad moved {cx - PAD_GEOM['cx']}, expected {mm(15)}"
    for s in range(3):
        dev.up(s)
    engine.process_frame(dev.frame())

    # vertical 4f: scale_overview = 2.0 (plain multiplier)
    for s in range(4):
        dev.down(s, 1000 + s * 200, 2000)
    engine.process_frame(dev.frame())
    for i in range(1, 9):
        clock.t += 0.01
        for s in range(4):
            dev.move(s, 1000 + s * 200, 2000 - int(mm(2.5)) * i)
        engine.process_frame(dev.frame())
    down, pos, keys = clone_state(pad)
    cy = sum(pos[s][1] for s in down) / 4
    assert abs(cy - (PAD_GEOM["cy"] - mm(40))) <= 3, \
        f"pad moved {cy - PAD_GEOM['cy']}, expected {-mm(40)}"
    print("ok  auto gains: 1:1 view scale and overview multiplier")


def flat_events(frames):
    return [e for f in frames for e in f]


def test_hold_to_move():
    clock = FakeClock()
    engine, emitted, acts = make_engine(
        clock, mode="native",
        calib={"u": 10.0, "w_mm": 512.0, "h_mm": 358.0})
    dev = FakeTouch()
    for s, (x, y) in enumerate([(1000, 900), (1200, 900), (1400, 1100)]):
        dev.down(s, x, y)
    engine.process_frame(dev.frame())
    # fingers rest still past hold_ms (350) -> move begins via tick()
    clock.t += 0.4
    engine.tick()
    kbd = flat_events(engine._kbd_events)
    ptr = flat_events(engine._ptr_events)
    assert (ec.EV_KEY, ntg.ec.KEY_LEFTMETA, 1) in kbd, f"Super down: {kbd}"
    assert (ec.EV_KEY, ec.BTN_LEFT, 1) in ptr, f"left button down: {ptr}"
    # warp lands at the centroid: (1200, 966.7) units -> mm/size normalized
    ax = [v for t, c, v in ptr if c == ec.ABS_X][0]
    exp = round(1200 / RES / 512.0 * ntg.PTR_RANGE)
    assert abs(ax - exp) <= 2, f"warp x {ax} != {exp}"
    # drag 30 mm right: pointer follows
    for i in range(1, 13):
        clock.t += 0.01
        for s, (x, y) in enumerate([(1000, 900), (1200, 900), (1400, 1100)]):
            dev.move(s, x + mm(2.5) * i, y)
        engine.process_frame(dev.frame())
    ax2 = [v for t, c, v in flat_events(engine._ptr_events)
           if c == ec.ABS_X][-1]
    exp_d = 30.0 / 512.0 * ntg.PTR_RANGE
    assert abs((ax2 - ax) - exp_d) <= 4, f"drag {ax2 - ax} != {exp_d}"
    # lifting two fingers keeps dragging; lifting all ends the move
    dev.up(0)
    dev.up(1)
    engine.process_frame(dev.frame())
    kbd = flat_events(engine._kbd_events)
    assert (ec.EV_KEY, ntg.ec.KEY_LEFTMETA, 0) not in kbd, "still dragging"
    dev.up(2)
    engine.process_frame(dev.frame())
    ptr = flat_events(engine._ptr_events)
    kbd = flat_events(engine._kbd_events)
    assert (ec.EV_KEY, ec.BTN_LEFT, 0) in ptr, "button must release"
    assert (ec.EV_KEY, ntg.ec.KEY_LEFTMETA, 0) in kbd, "Super must release"
    assert engine._pad_events == [], "no swipe may reach the pad"
    assert acts == [] and engine._closed == []
    print("ok  3-finger hold starts Mod+drag window move, lift ends it")


def test_pinch_close():
    clock = FakeClock()
    engine, emitted, acts = make_engine(
        clock, mode="native",
        calib={"u": 10.0, "w_mm": 512.0, "h_mm": 358.0})
    dev = FakeTouch()
    base = [(1000, 1000), (1400, 1000), (1200, 1400)]
    cx = sum(p[0] for p in base) / 3
    cy = sum(p[1] for p in base) / 3
    for s, (x, y) in enumerate(base):
        dev.down(s, x, y)
    engine.process_frame(dev.frame())
    # contract contacts to 10% of their distance from the centroid
    for i in range(1, 10):
        clock.t += 0.01
        k = 1.0 - 0.1 * i
        for s, (x, y) in enumerate(base):
            dev.move(s, int(cx + (x - cx) * k), int(cy + (y - cy) * k))
        engine.process_frame(dev.frame())
    assert engine._closed == [("close-window", 42)], \
        f"window 42 should close: {engine._closed}"
    assert engine._pad_events == [], "pinch must not reach the pad"
    assert engine._ptr_events == [], "pinch must not press buttons"
    # the rest of the touch is swallowed
    for s in range(3):
        dev.up(s)
    engine.process_frame(dev.frame())
    assert len(engine._closed) == 1, "close must fire exactly once"
    print("ok  3-finger pinch closes the window under the fingers")


def spread_run(fingers, action_expected):
    clock = FakeClock()
    engine, emitted, acts = make_engine(
        clock, mode="native",
        calib={"u": 10.0, "w_mm": 512.0, "h_mm": 358.0})
    dev = FakeTouch()
    base = [(1100, 1000), (1300, 1000), (1200, 1300), (1200, 850)][:fingers]
    cx = sum(p[0] for p in base) / fingers
    cy = sum(p[1] for p in base) / fingers
    for s, (x, y) in enumerate(base):
        dev.down(s, x, y)
    engine.process_frame(dev.frame())
    # spread contacts outward to 2.5x their distance from the centroid
    for i in range(1, 11):
        clock.t += 0.01
        k = 1.0 + 0.15 * i
        for s, (x, y) in enumerate(base):
            dev.move(s, int(cx + (x - cx) * k), int(cy + (y - cy) * k))
        engine.process_frame(dev.frame())
    for s in range(fingers):
        dev.up(s)
    engine.process_frame(dev.frame())
    assert engine._closed == [(action_expected, 42)], \
        f"{fingers}-finger spread: {engine._closed}"
    assert engine._pad_events == [], "spread must not reach the pad"
    assert acts == []


def pinch_run(fingers, action_expected):
    clock = FakeClock()
    engine, emitted, acts = make_engine(
        clock, mode="native",
        calib={"u": 10.0, "w_mm": 512.0, "h_mm": 358.0})
    dev = FakeTouch()
    base = [(1000, 1000), (1400, 1000), (1200, 1400), (1200, 700)][:fingers]
    cx = sum(p[0] for p in base) / fingers
    cy = sum(p[1] for p in base) / fingers
    for s, (x, y) in enumerate(base):
        dev.down(s, x, y)
    engine.process_frame(dev.frame())
    for i in range(1, 10):
        clock.t += 0.01
        k = 1.0 - 0.1 * i
        for s, (x, y) in enumerate(base):
            dev.move(s, int(cx + (x - cx) * k), int(cy + (y - cy) * k))
        engine.process_frame(dev.frame())
    for s in range(fingers):
        dev.up(s)
    engine.process_frame(dev.frame())
    assert engine._closed == [(action_expected, 42)], \
        f"{fingers}-finger pinch: {engine._closed}"


def test_spread_actions():
    spread_run(3, "spawn kitty")
    spread_run(4, "window-zoom-in")
    pinch_run(4, "window-zoom-out")
    print("ok  spread: terminal / zoom-in; 4-finger pinch: zoom-out")


def test_zoom_staging():
    # calibrated against the live Surface session: logical 1536x1024,
    # gaps 14, top bar strut 34
    assert ntg.zoom_stage(747, 962, 1536, 1024, 14) == 0
    assert ntg.zoom_stage(1508, 962, 1536, 1024, 14) == 1
    assert ntg.zoom_stage(1536, 990, 1536, 1024, 14) == 2
    assert ntg.zoom_stage(1536, 1024, 1536, 1024, 14) == 3
    assert ntg.zoom_action(True, 0, 5) == ["maximize-column"]
    assert ntg.zoom_action(True, 1, 5) == \
        ["maximize-window-to-edges", "--id", "5"]
    assert ntg.zoom_action(True, 2, 5) == ["fullscreen-window", "--id", "5"]
    assert ntg.zoom_action(True, 3, 5) is None
    assert ntg.zoom_action(False, 3, 5) == ["fullscreen-window", "--id", "5"]
    assert ntg.zoom_action(False, 2, 5) == \
        ["maximize-window-to-edges", "--id", "5"]
    assert ntg.zoom_action(False, 1, 5) == ["maximize-column"]
    assert ntg.zoom_action(False, 0, 5) is None
    print("ok  staged window zoom walks both directions")


def test_move_edge_zones():
    clock = FakeClock()
    engine, emitted, acts = make_engine(
        clock, mode="native",
        calib={"u": 10.0, "w_mm": 512.0, "h_mm": 358.0})
    dev = FakeTouch()
    pts = [(1000, 1800), (1200, 1800), (1400, 1600)]
    for s, (x, y) in enumerate(pts):
        dev.down(s, x, y)
    engine.process_frame(dev.frame())
    clock.t += 0.35
    engine.tick()  # hold fires -> move begins
    assert engine.sub == "move", f"expected move, got {engine.sub}"
    # drag up close to the top edge (centroid y -> ~30 units = 3 mm)
    steps = 20
    for i in range(1, steps + 1):
        clock.t += 0.01
        for s, (x, y) in enumerate(pts):
            dev.move(s, x, y - int((y - 90) * i / steps))
        engine.process_frame(dev.frame())
    assert engine._edge_moves == [], "no switch before the dwell delay"
    clock.t += 0.3
    engine.tick()
    assert engine._edge_moves == ["up"], f"got {engine._edge_moves}"
    clock.t += 0.65
    engine.tick()
    assert engine._edge_moves == ["up", "up"], "repeat while dwelling"
    for s in range(3):
        dev.up(s)
    engine.process_frame(dev.frame())
    print("ok  dragging to the top edge switches workspaces, with repeat")


CAL = {"u": 10.0, "w_mm": 512.0, "h_mm": 358.0}


def test_hold4_resize():
    clock = FakeClock()
    engine, emitted, acts = make_engine(clock, mode="native", calib=CAL)
    dev = FakeTouch()
    for s, (x, y) in enumerate([(1000, 900), (1200, 900),
                                (1400, 1100), (1200, 700)]):
        dev.down(s, x, y)
    engine.process_frame(dev.frame())
    clock.t += 0.35
    engine.tick()
    ptr = flat_events(engine._ptr_events)
    kbd = flat_events(engine._kbd_events)
    assert (ec.EV_KEY, ec.BTN_RIGHT, 1) in ptr, f"right button down: {ptr}"
    assert (ec.EV_KEY, ec.BTN_LEFT, 1) not in ptr, "not a move"
    assert (ec.EV_KEY, ntg.ec.KEY_LEFTMETA, 1) in kbd
    for s in range(4):
        dev.up(s)
    engine.process_frame(dev.frame())
    ptr = flat_events(engine._ptr_events)
    assert (ec.EV_KEY, ec.BTN_RIGHT, 0) in ptr, "right button must release"
    print("ok  4-finger hold starts Mod+right-drag resize")


def test_five_tap():
    clock = FakeClock()
    engine, emitted, acts = make_engine(clock, mode="native", calib=CAL)
    dev = FakeTouch()
    pts = [(1000, 900), (1200, 900), (1400, 1100), (1200, 700), (1600, 900)]
    for s, (x, y) in enumerate(pts):
        dev.down(s, x, y)
    engine.process_frame(dev.frame())
    clock.t += 0.12
    for s in range(5):
        dev.up(s)
    engine.process_frame(dev.frame())
    assert engine._closed == [("spawn noctalia msg panel-toggle launcher",
                               None)], f"five tap: {engine._closed}"
    print("ok  5-finger tap opens the launcher")


def test_slow_drift_still_holds():
    """Slow finger drift must arm the hold, not engage a swipe."""
    clock = FakeClock()
    engine, emitted, acts = make_engine(clock, mode="native", calib=CAL)
    dev = FakeTouch()
    pts = [(1000, 900), (1200, 900), (1400, 1100)]
    for s, (x, y) in enumerate(pts):
        dev.down(s, x, y)
    engine.process_frame(dev.frame())
    # drift right at ~20 mm/s (1.2 mm per 60 ms frame)
    for i in range(1, 5):
        clock.t += 0.06
        for s, (x, y) in enumerate(pts):
            dev.move(s, x + int(mm(1.2)) * i, y)
        engine.process_frame(dev.frame())
    assert engine.sub == "move", \
        f"drift should become a hold, got sub={engine.sub}"
    ptr = flat_events(engine._ptr_events)
    assert (ec.EV_KEY, ec.BTN_LEFT, 1) in ptr
    assert engine._pad_events == [], "no swipe may engage"
    for s in range(3):
        dev.up(s)
    engine.process_frame(dev.frame())
    print("ok  slow drift arms the hold instead of swiping")


def test_three_double_tap():
    clock = FakeClock()
    engine, emitted, acts = make_engine(clock, mode="native", calib=CAL)
    dev = FakeTouch()
    pts = [(1000, 900), (1200, 900), (1400, 1100)]

    def tap():
        for s, (x, y) in enumerate(pts):
            dev.down(s, x, y)
        engine.process_frame(dev.frame())
        clock.t += 0.1
        for s in range(3):
            dev.up(s)
        engine.process_frame(dev.frame())

    tap()
    assert engine._closed == [], "single tap must do nothing"
    clock.t += 0.2
    tap()
    assert engine._closed == [("toggle-window-floating", 42)], \
        f"double tap: {engine._closed}"
    print("ok  3-finger double tap toggles floating")


def test_five_pinch_screenshot():
    clock = FakeClock()
    engine, emitted, acts = make_engine(clock, mode="native", calib=CAL)
    dev = FakeTouch()
    base = [(1000, 900), (1400, 900), (1200, 1400), (1000, 1400), (1400, 600)]
    cx = sum(p[0] for p in base) / 5
    cy = sum(p[1] for p in base) / 5
    for s, (x, y) in enumerate(base):
        dev.down(s, x, y)
    engine.process_frame(dev.frame())
    for i in range(1, 10):
        clock.t += 0.01
        k = 1.0 - 0.1 * i
        for s, (x, y) in enumerate(base):
            dev.move(s, int(cx + (x - cx) * k), int(cy + (y - cy) * k))
        engine.process_frame(dev.frame())
    assert engine._closed == [("screenshot", 42)], \
        f"5-finger pinch: {engine._closed}"
    print("ok  5-finger pinch takes a screenshot")


def test_strip_volume_and_replay():
    clock = FakeClock()
    engine, emitted, acts = make_engine(clock, mode="native", calib=CAL)
    dev = FakeTouch()
    # right edge: 510 mm of 512 -> raw x 5100; drag up 14 mm
    dev.down(0, 5100, 2000)
    engine.process_frame(dev.frame())
    for i in range(1, 8):
        clock.t += 0.01
        dev.move(0, 5100, 2000 - 20 * i)  # 2 mm per frame
        engine.process_frame(dev.frame())
    dev.up(0)
    engine.process_frame(dev.frame())
    # fader: base 40, armed after 6 mm, 8 mm more at 100/80 per mm -> 50
    assert engine._strips, "fader must send levels"
    assert all(s == "right" for s, v in engine._strips)
    vals = [v for s, v in engine._strips]
    assert vals == sorted(vals), f"levels must rise monotonically: {vals}"
    assert vals[-1] == 50, f"final level 50 expected: {vals}"
    assert emitted == [], "strip touches must never reach apps"

    # a tap on the left edge is replayed to the app untouched
    dev2 = FakeTouch()
    dev2.down(0, 10, 1500)
    engine.process_frame(dev2.frame())
    clock.t += 0.05
    dev2.up(0)
    engine.process_frame(dev2.frame())
    down, pos, keys = clone_state(emitted)
    assert pos.get(0) == [10, 1500], f"edge tap must replay: {pos}"
    assert keys.get(ec.BTN_TOUCH) == 0
    assert [v for s, v in engine._strips][-1] == 50, "no extra strip events"
    print("ok  right-edge fader slides volume; edge taps pass through")


def test_four_tap_and_media_swipe():
    clock = FakeClock()
    engine, emitted, acts = make_engine(clock, mode="native", calib=CAL)
    dev = FakeTouch()
    pts = [(1000, 900), (1200, 900), (1400, 1100), (1200, 700)]
    # 4-finger tap -> play/pause
    for s, (x, y) in enumerate(pts):
        dev.down(s, x, y)
    engine.process_frame(dev.frame())
    clock.t += 0.12
    for s in range(4):
        dev.up(s)
    engine.process_frame(dev.frame())
    assert engine._closed == [("spawn noctalia msg media toggle", None)], \
        f"four tap: {engine._closed}"

    # fast 4-finger swipe left -> next track, and nothing reaches the pad
    engine._closed.clear()
    for s, (x, y) in enumerate(pts):
        dev.down(s, x, y)
    engine.process_frame(dev.frame())
    for i in range(1, 6):
        clock.t += 0.01
        for s, (x, y) in enumerate(pts):
            dev.move(s, x - mm(2.5) * i, y)
        engine.process_frame(dev.frame())
    for s in range(4):
        dev.up(s)
    engine.process_frame(dev.frame())
    assert engine._closed == [("spawn noctalia msg media previous", None)], \
        f"swipe4 left: {engine._closed}"
    assert engine._pad_events == [], "media swipe must not reach the pad"
    print("ok  4-finger tap toggles playback, swipe left goes back a track")


def make_tp_engine(clock):
    import copy
    cfg = copy.deepcopy(ntg.DEFAULT_CONFIG)
    emitted = []
    rels = []
    kbds = []
    pinches = []
    engine = ntg.TrackpadEngine(
        cfg,
        emit=lambda evs: emitted.append(list(evs)),
        mm_x=RES, mm_y=RES,
        emit_kbd=lambda evs: kbds.append(list(evs)),
        emit_rel=lambda evs: rels.append(list(evs)),
        pinch_action=lambda a, w: pinches.append((a, w)),
        calib={"u": 10.0, "w_mm": 100.0, "h_mm": 60.0},
        move_gain=1.0,
    )
    engine._emitted = emitted
    engine._rels = rels
    engine._kbds = kbds
    engine._pinches = pinches
    return engine


def test_tp_swipe_replay():
    """A fast 3-finger swipe reaches the clone verbatim (native niri)."""
    clock = FakeClock()
    engine = make_tp_engine(clock)
    dev = FakeTouch()
    dev.down(0, 300, 300)
    engine.process_frame(dev.frame())   # forwarded live
    dev.down(1, 500, 300)
    engine.process_frame(dev.frame())
    dev.down(2, 700, 300)               # buffering starts
    engine.process_frame(dev.frame())
    for i in range(1, 7):
        clock.t += 0.01
        for s in range(3):
            dev.move(s, 300 + s * 200 + mm(2.5) * i, 300)
        engine.process_frame(dev.frame())
    down, pos, keys = clone_state(engine._emitted)
    assert len(down) == 3, f"clone must see all 3 fingers: {down}"
    assert pos[2][0] == 700 + mm(15), f"replayed positions wrong: {pos}"
    for s in range(3):
        dev.up(s)
    engine.process_frame(dev.frame())
    down, pos, keys = clone_state(engine._emitted)
    assert not down and keys.get(ec.BTN_TOUCH) == 0
    assert engine._pinches == [] and engine._rels == []
    print("ok  trackpad: native swipe replays to the clone")


def test_tp_pinch_claim():
    """A pinch cancels the forwarded contacts and fires the action."""
    clock = FakeClock()
    engine = make_tp_engine(clock)
    dev = FakeTouch()
    base = [(300, 300), (700, 300), (500, 700)]
    dev.down(0, *base[0])
    engine.process_frame(dev.frame())   # forwarded: clone has 1 contact
    dev.down(1, *base[1])
    engine.process_frame(dev.frame())
    dev.down(2, *base[2])
    engine.process_frame(dev.frame())
    cx = sum(p[0] for p in base) / 3
    cy = sum(p[1] for p in base) / 3
    for i in range(1, 10):
        clock.t += 0.01
        k = 1.0 - 0.1 * i
        for s, (x, y) in enumerate(base):
            dev.move(s, int(cx + (x - cx) * k), int(cy + (y - cy) * k))
        engine.process_frame(dev.frame())
    assert engine._pinches == [("close-window", None)], \
        f"pinch: {engine._pinches}"
    down, pos, keys = clone_state(engine._emitted)
    assert not down, f"forwarded contacts must be cancelled: {down}"
    assert keys.get(ec.BTN_TOUCH) == 0
    for s in range(3):
        dev.up(s)
    engine.process_frame(dev.frame())
    assert len(engine._pinches) == 1
    print("ok  trackpad: pinch claims and cancels cleanly")


def test_tp_taps():
    clock = FakeClock()
    engine = make_tp_engine(clock)
    dev = FakeTouch()
    # 4-finger tap -> play/pause, swallowed
    for s, x in enumerate((300, 500, 700, 900)):
        dev.down(s, x, 300)
    engine.process_frame(dev.frame())
    clock.t += 0.1
    for s in range(4):
        dev.up(s)
    engine.process_frame(dev.frame())
    assert engine._pinches == [("spawn noctalia msg media toggle", None)]
    assert engine._emitted == [], "4-finger tap must be swallowed"

    # 3-finger tap -> replayed so libinput's middle-click tap still works
    engine._pinches.clear()
    for s, x in enumerate((300, 500, 700)):
        dev.down(s, x, 300)
    engine.process_frame(dev.frame())
    clock.t += 0.1
    for s in range(3):
        dev.up(s)
    engine.process_frame(dev.frame())
    assert engine._pinches == [], "3-finger tap is libinput's"
    down, pos, keys = clone_state(engine._emitted)
    assert not down and pos.get(0) == [300, 300], \
        f"3-finger tap must replay: {pos}"
    print("ok  trackpad: 4-tap claims, 3-tap replays for middle click")


def test_tp_hold_and_media_swipe():
    clock = FakeClock()
    engine = make_tp_engine(clock)
    dev = FakeTouch()
    # hold -> Super + left button on the relative mouse, motion in REL
    for s, x in enumerate((300, 500, 700)):
        dev.down(s, x, 300)
    engine.process_frame(dev.frame())
    clock.t += 0.35
    engine.tick()
    kbd = flat_events(engine._kbds)
    rel = flat_events(engine._rels)
    assert (ec.EV_KEY, ntg.ec.KEY_LEFTMETA, 1) in kbd
    assert (ec.EV_KEY, ec.BTN_LEFT, 1) in rel
    for i in range(1, 5):
        clock.t += 0.01
        for s, x in enumerate((300, 500, 700)):
            dev.move(s, x + mm(2.5) * i, 300)
        engine.process_frame(dev.frame())
    moved = sum(v for t, c, v in flat_events(engine._rels) if c == ec.REL_X)
    exp = 10.0 * (1000.0 / 25.4)  # 10 mm in libinput-normalized units
    assert abs(moved - exp) <= 2, f"REL motion {moved} != {exp:.0f}"
    for s in range(3):
        dev.up(s)
    engine.process_frame(dev.frame())
    rel = flat_events(engine._rels)
    kbd = flat_events(engine._kbds)
    assert (ec.EV_KEY, ec.BTN_LEFT, 0) in rel
    assert (ec.EV_KEY, ntg.ec.KEY_LEFTMETA, 0) in kbd

    # fast 4-finger horizontal swipe -> media next, swallowed
    engine._pinches.clear()
    n_fwd = len(engine._emitted)
    for s, x in enumerate((300, 500, 700, 900)):
        dev.down(s, x, 500)
    engine.process_frame(dev.frame())
    for i in range(1, 6):
        clock.t += 0.01
        for s, x in enumerate((300, 500, 700, 900)):
            dev.move(s, x - mm(2.5) * i, 500)
        engine.process_frame(dev.frame())
    for s in range(4):
        dev.up(s)
    engine.process_frame(dev.frame())
    assert engine._pinches == [("spawn noctalia msg media previous", None)], \
        f"media swipe: {engine._pinches}"
    assert len(engine._emitted) == n_fwd, "media swipe must be swallowed"
    print("ok  trackpad: hold drags via relative mouse, media swipe claims")


def test_tp_pinch_count_scaling():
    clock = FakeClock()
    engine = make_tp_engine(clock)
    g = {"pinch_close_mm": 5.0, "pinch_out_mm": 5.0}
    assert engine._pinch_thresholds(g, 3) == (5.0, 5.0)
    assert engine._pinch_thresholds(g, 4) == (3.75, 3.75)
    assert engine._pinch_thresholds(g, 5) == (3.0, 3.0)
    print("ok  trackpad pinch thresholds scale with finger count")


def test_niri_config_parse():
    import tempfile
    sample = """
input {
    touchpad {
        natural-scroll
        accel-speed 0.2
        // accel-profile "adaptive"
        accel-profile "flat"
    }
    mouse {
        accel-profile "adaptive"
        accel-speed -0.5
    }
}
"""
    with tempfile.NamedTemporaryFile("w", suffix=".kdl", delete=False) as f:
        f.write(sample)
        path = f.name
    try:
        profile, speed = ntg.parse_niri_touchpad_accel(path)
    finally:
        import os
        os.unlink(path)
    assert profile == "flat", f"profile {profile}"
    assert abs(speed - 0.2) < 1e-9, f"speed {speed}"
    u = ntg.libinput_units_per_mm(profile, speed)
    assert abs(u - (1000 / 25.4) * 0.2968 * 1.2) < 1e-6, f"units {u}"
    u = ntg.libinput_units_per_mm("adaptive", 0.7)
    assert abs(u - (1000 / 25.4) * 0.9 * 0.2968) < 1e-6, \
        "adaptive must ignore the speed setting"
    print("ok  niri config parse and libinput unit math")


def main():
    real_monotonic = time.monotonic
    clock = FakeClock()
    # the engine reads time.monotonic directly; give tests a steerable clock
    tests = [
        test_single_finger_passthrough,
        test_three_finger_swipe_right_and_cancel,
        test_three_finger_swipe_down,
        test_four_finger_up_then_reverse,
        test_flick,
        test_deadzone_no_fire,
        test_clone_cache_resync,
        test_native_mirror_lifecycle,
        test_native_below_deadzone_quiet,
        test_native_finger_count_transition,
        test_auto_gains,
        test_hold_to_move,
        test_pinch_close,
        test_spread_actions,
        test_zoom_staging,
        test_move_edge_zones,
        test_hold4_resize,
        test_five_tap,
        test_slow_drift_still_holds,
        test_three_double_tap,
        test_five_pinch_screenshot,
        test_strip_volume_and_replay,
        test_four_tap_and_media_swipe,
        test_tp_swipe_replay,
        test_tp_pinch_claim,
        test_tp_taps,
        test_tp_hold_and_media_swipe,
        test_tp_pinch_count_scaling,
        test_niri_config_parse,
    ]
    failures = 0
    for t in tests:
        clock.t = 1000.0
        time.monotonic = clock  # noqa: shared steerable clock

        # rebind the clock used inside each test
        import inspect
        try:
            # tests create their own FakeClock; patch module clock to it by
            # intercepting: simplest is to run and let both clocks advance.
            t.__globals__["FakeClock"] = lambda c=clock: c
            t()
        except AssertionError as exc:
            failures += 1
            print(f"FAIL {t.__name__}: {exc}")
        finally:
            time.monotonic = real_monotonic
            t.__globals__["FakeClock"] = FakeClock
    if failures:
        print(f"\n{failures} test(s) failed")
        return 1
    print("\nall tests passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
