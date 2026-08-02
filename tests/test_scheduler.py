from pathlib import Path

import pytest

from cambium.downlink.packetize import FLAG_MICRO_LEASE, HeaderStamper, frame_to_packets
from cambium.downlink.scheduler import TxScheduler
from cambium.model import FixtureFrame, RGBW
from cambium.roster import Roster
from cambium.wire.packets import DirectFrame, parse_packet

BENCH10 = Path(__file__).parent.parent / "config" / "roster-bench10.csv"

INTERVAL = 0.125  # 1 / tx_hz at the intended 8.0 Hz (binary-exact float)


class FakeTime:
    """Deterministic clock + sleep for driving run() without real time.

    sleep() advances the fake clock instantly and, once `budget` sleeps have
    happened, calls stop() on the scheduler -- so tests await run() directly
    (no background tasks, no races). on_sleep(n) lets a test inject frames
    mid-run at an exact tick.
    """

    def __init__(self):
        self.now = 0.0
        self.sleeps: list[float] = []
        self.budget: int | None = None
        self.stop = lambda: None
        self.on_sleep = None

    def clock(self):
        return self.now

    async def sleep(self, dt):
        self.sleeps.append(dt)
        self.now += dt
        if self.on_sleep is not None:
            self.on_sleep(len(self.sleeps))
        if self.budget is not None and len(self.sleeps) >= self.budget:
            self.stop()


def make_sched(ft, packetize, tx_hz=8.0, stale=2.0):
    sent: list[tuple[float, bytes]] = []

    async def send(raw):
        sent.append((ft.now, raw))

    sched = TxScheduler(
        send, packetize, tx_hz, stale, clock=ft.clock, sleep=ft.sleep
    )
    ft.stop = sched.stop
    return sched, sent


def one_packet(frame):
    return [b"pkt"]


# ---- rate cap --------------------------------------------------------------

async def test_rate_cap_respected():
    ft = FakeTime()
    sched, sent = make_sched(ft, one_packet)
    sched.set_frame(FixtureFrame(seq=1))
    ft.budget = 8
    await sched.run()
    # 8 sends across exactly 1.0 s of fake time: the 8 Hz cap holds.
    assert len(sent) == 8
    assert ft.now == 1.0
    times = [t for t, _ in sent]
    assert [round(b - a, 9) for a, b in zip(times, times[1:])] == [INTERVAL] * 7


# ---- latest-wins mailbox ---------------------------------------------------

async def test_latest_wins_drops_intermediate_frames():
    ft = FakeTime()
    seen: list[FixtureFrame] = []

    def packetize(frame):
        seen.append(frame)
        return [b"pkt"]

    sched, sent = make_sched(ft, packetize)
    a, b = FixtureFrame(seq=1), FixtureFrame(seq=2)
    c, d = FixtureFrame(seq=3), FixtureFrame(seq=4)
    sched.set_frame(a)
    sched.set_frame(b)  # replaces a before the first tick

    def inject(n_sleeps):
        if n_sleeps == 1:  # between tick 1 and tick 2
            sched.set_frame(c)
            sched.set_frame(d)  # replaces c before tick 2

    ft.on_sleep = inject
    ft.budget = 2
    await sched.run()
    assert [f.seq for f in seen] == [2, 4]  # a and c never hit the radio
    assert sched.stats.frames_in == 4
    assert sched.stats.packets_out == 2


# ---- stale input -> silence ------------------------------------------------

async def test_stale_input_goes_silent():
    ft = FakeTime()
    sched, sent = make_sched(ft, one_packet, stale=2.0)
    sched.set_frame(FixtureFrame(seq=1))
    ft.budget = 24  # 3.0 s of fake time
    await sched.run()
    # Ticks at t=0..2.0 (age <= stale) send; ticks after that are silent --
    # the fixtures' hold/fallback ladder takes over, cambium never fights it.
    assert len(sent) == 17
    assert max(t for t, _ in sent) == 2.0
    assert sched.stats.ticks_idle == 7
    assert sched.stats.packets_out == 17


async def test_no_frame_ever_set_sends_nothing():
    ft = FakeTime()
    sched, sent = make_sched(ft, one_packet)
    ft.budget = 3
    await sched.run()
    assert sent == []
    assert sched.stats.ticks_idle == 3


async def test_empty_packetize_still_paces_the_loop():
    ft = FakeTime()
    sched, sent = make_sched(ft, lambda frame: [])
    sched.set_frame(FixtureFrame(seq=1))
    ft.budget = 2
    await sched.run()
    assert sent == []
    assert ft.sleeps == [INTERVAL, INTERVAL]  # no busy-spin on empty output


# ---- stagger ---------------------------------------------------------------

async def test_packets_staggered_across_the_tick():
    ft = FakeTime()
    sched, sent = make_sched(ft, lambda frame: [b"p%d" % i for i in range(7)])
    sched.set_frame(FixtureFrame(seq=1))
    ft.budget = 7  # exactly one tick's worth of stagger sleeps
    await sched.run()
    assert len(sent) == 7
    spacing = INTERVAL / 7  # ~17.9 ms between packets, no burst
    assert ft.sleeps == [spacing] * 7
    times = [t for t, _ in sent]
    # approx: accumulated sums differ from i*spacing in the last float ulp
    assert times == pytest.approx([i * spacing for i in range(7)])
    assert times[-1] < INTERVAL  # whole frame lands inside one interval


# ---- oneshot passthrough ---------------------------------------------------

async def test_oneshot_sends_immediately_without_run():
    ft = FakeTime()
    sched, sent = make_sched(ft, one_packet)
    await sched.send_oneshot(b"identify")  # no run(), no frame, no tick
    assert sent == [(0.0, b"identify")]
    assert sched.stats.packets_out == 1


# ---- wiring with the real packetizer ---------------------------------------

async def test_real_packetize_wiring_end_to_end():
    roster = Roster.load(BENCH10)
    stamper = HeaderStamper(clock=lambda: 0.0)
    ft = FakeTime()
    sched, sent = make_sched(
        ft, lambda frame: frame_to_packets(frame, roster, stamper)
    )
    sched.set_frame(
        FixtureFrame(seq=1, colors={"F2BDB4": RGBW(10, 20, 30, 40)})
    )
    ft.budget = 1
    await sched.run()
    (item,) = sent
    df = parse_packet(item[1])
    assert isinstance(df, DirectFrame)
    assert df.flags & FLAG_MICRO_LEASE
    assert df.count == 1
    assert (df.entries[0].r, df.entries[0].w) == (10, 40)
