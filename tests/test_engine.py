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

import niri_touch_gestures as ntg
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


def make_engine(clock, mode="discrete"):
    import copy
    cfg = copy.deepcopy(ntg.DEFAULT_CONFIG)
    cfg["gestures"]["mode"] = mode
    emitted = []
    acts = []
    pad_events = []
    engine = ntg.GestureEngine(
        cfg,
        emit=lambda evs: emitted.append(list(evs)),
        act=lambda n, d: acts.append((n, d)),
        mm_x=RES, mm_y=RES,
        transform_getter=lambda: "normal",
        emit_pad=(lambda evs: pad_events.append(list(evs)))
                 if mode == "native" else None,
        pad_geom=PAD_GEOM if mode == "native" else None,
    )
    engine._pad_events = pad_events
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
    # mirrored contacts are recentered around the pad center at engage;
    # by now we are (10 mm - engage point) past it, well under the deadzone
    cx = sum(p[0] for p in pos.values() if p[0] is not None) / 3
    assert abs(cx - PAD_GEOM["cx"]) <= mm(4), f"centroid {cx} far from center"

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
