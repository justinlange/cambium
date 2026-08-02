"""VirtualFixture RX ladder vs the production packet builders.

Two independent implementations meet here: packets are BUILT with
cambium.wire.packets (the production packer) and PARSED by fixture_sim's
hand-rolled ladder. Agreement is the proof that both match packet.h.
"""

from cambium.fakefleet.fixture_sim import VirtualFixture
from cambium.model import RGBW, FixtureClass
from cambium.wire.packets import (
    BROADCAST_ID,
    NbHeader,
    direct_frame,
    force_lifecycle,
    identify,
    program_set,
    short_id_from_str,
    show_frame,
)


class Clock:
    def __init__(self, t: float = 100.0) -> None:
        self.t = t

    def __call__(self) -> float:
        return self.t


def h() -> NbHeader:
    return NbHeader(src_id=short_id_from_str("FAB000"))


def vf(cls=FixtureClass.DOWNLIGHT, night=False, clock=None):
    return VirtualFixture(
        "0A0001", cls, (0.0, 0.0, 2.5), clock=clock or Clock(), start_night=night
    )


ME = short_id_from_str("0A0001")
OTHER = short_id_from_str("0A0002")


# ---------------------------------------------------------------------------
# Identify: renders in ANY lifecycle state; secs=0 cancels; color 0 = none
# ---------------------------------------------------------------------------

def test_identify_renders_in_day():
    f = vf(night=False)
    f.consume(identify(h(), ME, 10, color=5))
    assert f.pixels() == [RGBW(255, 255, 255, 255)]  # white, RGBW class
    assert f.identify is not None


def test_identify_perimeter_white_has_no_w_and_37_px():
    f = vf(cls=FixtureClass.PERIMETER)
    f.consume(identify(h(), ME, 10, color=5))
    px = f.pixels()
    assert len(px) == 37 and px[0] == RGBW(255, 255, 255, 0)


def test_identify_legacy_17b_is_status_led_only():
    f = vf()
    raw = identify(h(), ME, 10, color=5)[:17]  # truncate the color tail
    f.consume(raw)
    # color defaults 0 = blink pattern on the status LED; main px not driven
    assert f.identify is None or f.identify["color"] == 0
    assert f.pixels() != [RGBW(255, 255, 255, 255)]


def test_identify_secs_expiry_and_cancel():
    clock = Clock()
    f = vf(clock=clock)
    f.consume(identify(h(), ME, 2, color=1))
    assert f.pixels() == [RGBW(255, 0, 0, 0)]
    clock.t += 2.5
    assert f.identify is None
    # re-light then cancel with secs=0 (net_peer.cpp: until = now)
    f.consume(identify(h(), ME, 10, color=1))
    f.consume(identify(h(), ME, 0, color=0))
    assert f.identify is None


def test_identify_blink_halves_duty():
    clock = Clock(100.0)
    f = vf(clock=clock)
    f.consume(identify(h(), ME, 10, color=2, blink=1))
    assert f.pixels() == [RGBW(0, 255, 0, 0)]  # int(200.0*... ) even -> on
    clock.t = 100.5
    assert f.pixels() == [RGBW(0, 0, 0, 0)]  # off phase


def test_identify_targeting():
    f = vf()
    f.consume(identify(h(), OTHER, 10, color=5))
    assert f.identify is None and f.not_mine == 1
    f.consume(identify(h(), BROADCAST_ID, 10, color=5))
    assert f.identify is not None


# ---------------------------------------------------------------------------
# Night gate + lease: show/direct refused in day, need bit0 or a live lease
# ---------------------------------------------------------------------------

def test_direct_frame_day_gated():
    f = vf(night=False)
    f.consume(direct_frame(h(), [(ME, 10, 20, 30, 0)], flags=0x01))
    assert f.gated is True
    f.tick()
    assert f.pixels()[0] != RGBW(10, 20, 30, 0)  # autonomous, not commanded


def test_direct_frame_renders_at_night_with_slew():
    clock = Clock()
    f = vf(night=True, clock=clock)
    f.consume(direct_frame(h(), [(ME, 255, 0, 0, 0)], flags=0x01))
    f.tick()
    first = f.pixels()[0]
    assert first.r <= 32 + 32  # slew: at most one 32-step past start
    for _ in range(10):  # 10 ticks at 32/step covers 0..255
        f.tick()
    assert f.pixels()[0] == RGBW(255, 0, 0, 0)


def test_direct_frame_hard_cut_is_immediate():
    f = vf(night=True)
    f.consume(direct_frame(h(), [(ME, 200, 100, 50, 25)], flags=0x03))
    f.tick()
    assert f.pixels()[0] == RGBW(200, 100, 50, 25)


def test_direct_frame_without_lease_bit_ignored_until_leased():
    clock = Clock()
    f = vf(night=True, clock=clock)
    f.consume(direct_frame(h(), [(ME, 9, 9, 9, 0)], flags=0x00))  # no grant
    f.tick()
    assert f.pixels()[0] != RGBW(9, 9, 9, 0)
    # a program lease opens the door for grant-less frames
    f.consume(program_set(h(), ME, 3, 30))
    f.consume(direct_frame(h(), [(ME, 9, 9, 9, 0)], flags=0x02))  # hard-cut
    f.tick()
    assert f.pixels()[0] == RGBW(9, 9, 9, 0)


def test_direct_frame_entry_scan_ignores_frames_for_others():
    f = vf(night=True)
    f.consume(direct_frame(h(), [(OTHER, 1, 2, 3, 0)], flags=0x03))
    f.tick()
    assert f.pixels()[0] != RGBW(1, 2, 3, 0)
    assert f.not_mine == 1


def test_direct_frame_perimeter_forces_w0_uniform_wash():
    f = vf(cls=FixtureClass.PERIMETER, night=True)
    f.consume(direct_frame(h(), [(ME, 50, 60, 70, 255)], flags=0x03))
    f.tick()
    px = f.pixels()
    assert len(px) == 37
    assert all(p == RGBW(50, 60, 70, 0) for p in px)  # wash; W stripped


def test_hold_half_then_autonomous():
    clock = Clock()
    f = vf(night=True, clock=clock)
    f.consume(direct_frame(h(), [(ME, 200, 0, 0, 0)], flags=0x03))
    f.tick()
    assert f.pixels()[0] == RGBW(200, 0, 0, 0)
    clock.t += 2.0  # 1 s < age <= 3 s: hold at HALF, never blank
    f.tick()
    assert f.pixels()[0] == RGBW(100, 0, 0, 0)
    clock.t += 2.0  # age > 3 s: autonomous fallback (amber breathing)
    f.tick()
    p = f.pixels()[0]
    assert p != RGBW(0, 0, 0, 0) and p.r > p.g > p.b  # amber-ish, not blank


def test_showframe_night_gate_and_legacy_val_default():
    f = vf(night=True)
    # legacy 17 B: val defaults 255; hue 0 -> full red, micro-lease bit set
    f.consume(show_frame(h(), 0, 0, flags=0x01, legacy=True))
    f.consume(direct_frame(h(), [(ME, 0, 0, 0, 0)], flags=0x00)[:14])  # noise
    for _ in range(10):
        f.tick()
    assert f.pixels()[0] == RGBW(255, 0, 0, 0)
    day = vf(night=False)
    day.consume(show_frame(h(), 0, 0, flags=0x01, legacy=True))
    assert day.gated is True


# ---------------------------------------------------------------------------
# Force lifecycle + drops
# ---------------------------------------------------------------------------

def test_force_lifecycle_modes_and_targeting():
    f = vf(night=False)
    f.consume(force_lifecycle(h(), OTHER, 1))
    assert f.night is False  # not mine
    f.consume(force_lifecycle(h(), BROADCAST_ID, 1))
    assert f.night is True and f.life_state == "night"
    f.consume(force_lifecycle(h(), ME, 0))
    assert f.night is False
    f.consume(force_lifecycle(h(), ME, 2))  # auto -> boot default (day here)
    assert f.night is False


def test_night_clears_gated_flag():
    f = vf(night=False)
    f.consume(direct_frame(h(), [(ME, 1, 1, 1, 0)], flags=0x01))
    assert f.gated is True
    f.consume(force_lifecycle(h(), ME, 1))
    assert f.gated is False


def test_short_and_bad_ver_drops_counted():
    f = vf()
    f.consume(b"\x01\x02")  # < 13
    f.consume(b"\x07" + bytes(20))  # ver 7
    raw = identify(h(), ME, 10, color=5)
    f.consume(raw[:15])  # identify under its 17 B minimum
    assert (f.drop_short, f.drop_bad_ver) == (2, 1)
    assert f.identify is None
