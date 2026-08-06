"""VirtualFixture: a firmware-faithful software lantern.

This is the fake fleet's core: one instance behaves, at the packet level,
like a real fixture running Ben's firmware
(beneckart/resonance-lighting/firmware/fixture). It is the test
rig, the zero-hardware demo, and the FTUX rehearsal stage all at once.

Fidelity doctrine: the RX ladder below is HAND-ROLLED with literal struct
offsets transcribed from packet.h -- it deliberately does NOT import
cambium.wire.packets for parsing, so a codec bug in the production packer
cannot self-confirm by round-tripping through itself. Tests build packets
with the production builders and feed them here; agreement between the two
independent implementations is the proof.

Firmware semantics mirrored (with sources):
- length < 13 or ver != 1 -> silent drop, counted        (espnow_link.cpp RX gate)
- target_id 00:00:00 = all, else exact 3-byte match      (packet.h nbTargetMatches)
- NB_IDENTIFY renders in ANY lifecycle state             (fixture.ino renderTick:
  identify > smoke > night-show), color 0 = status-LED blink pattern only
  (main pixels NOT driven), secs=0 cancels immediately   (net_peer.cpp: until =
  millis() + secs*1000)
- NB_SHOWFRAME / NB_DIRECT_FRAME are NIGHT-GATED         (behavior_glue.cpp
  LIFE_NIGHT_SHOW gate) and lease-gated: flags bit0 grants a 10 s micro-lease
  (runtime.cpp noteShowFrame; frames without bit0 and no live lease are
  ignored)
- staleness ladder while leased: <=1 s hold, 1..3 s hold at half value,
  >3 s autonomous fallback -- never blank                (program.h
  RES_SHOWFRAME_HOLD_MS / RES_SHOWFRAME_STALE_MS)
- direct-frame slew: max 32/channel per 10 Hz render tick, flags bit1
  (hard-cut) bypasses                                    (prog_direct.cpp)
- NB_FORCE_LIFECYCLE: mode 0=force day 1=force night 2=auto, RAM-only
"""

from __future__ import annotations

import math
import struct
import time

from cambium.model import RGBW, FixtureClass

# NbType values consumed here (packet.h registry).
_T_SHOWFRAME = 2
_T_IDENTIFY = 6
_T_PROGRAM_SET = 19
_T_DIRECT_FRAME = 25
_T_FORCE_LIFECYCLE = 26

# packet.h timing constants (ms in firmware; seconds here).
_HOLD_S = 1.0        # RES_SHOWFRAME_HOLD_MS
_STALE_S = 3.0       # RES_SHOWFRAME_STALE_MS
_MICROLEASE_S = 10.0 # RES_SHOWFRAME_MICROLEASE_MS
_SLEW_STEP = 32      # prog_direct.cpp per-tick channel step

# NbIdentify color codes -> rendered pixel color (led_driver.cpp
# ledIdentifyFrame): 0=none (status-LED blink pattern, main px not driven).
_IDENT_COLORS = {
    1: (255, 0, 0),
    2: (0, 255, 0),
    3: (0, 0, 255),
    4: (255, 255, 0),
    5: (255, 255, 255),
}

# Heartbeat truncation boundaries (packet.h NB_HB_SHORT_LEN / sizeof).
_HB_SHORT_LEN = 29
_HB_FULL_LEN = 148
# Literal offsets into NbHeartbeat, transcribed from packet.h field order.
_HB_OFF_BATT_MV = 13    # int16
_HB_OFF_BATT_MA = 15    # int16
_HB_OFF_SOC = 17        # u8
_HB_OFF_MODE = 20       # u8
_HB_OFF_DL_RSSI = 23    # int8
_HB_OFF_FW_REV = 59     # char[24]
_HB_OFF_LIFE_STATE = 143  # u8 (tail 13: profile@142, life_state@143,
_HB_OFF_POWER_TIER = 144  #     power_tier@144, active_program@145)


def _clamp_step(current: int, target: int, step: int) -> int:
    if target > current:
        return min(current + step, target)
    return max(current - step, target)


class VirtualFixture:
    """One software lantern. Drive it with consume(); read pixels()."""

    def __init__(
        self,
        mac: str,
        cls: FixtureClass,
        xyz: tuple[float, float, float] | None = None,
        *,
        clock=time.monotonic,
        start_night: bool = False,
    ) -> None:
        self.mac = mac
        self.cls = cls
        self.xyz = xyz
        self._clock = clock
        self._id3 = bytes.fromhex(mac)

        # Lifecycle: real fixtures boot in daytime (the #1 bench trap --
        # rehearsed here on purpose). force: None=auto, 0=day, 1=night.
        self._auto_night = start_night
        self._force: int | None = None

        # Identify hold (renders in any lifecycle state).
        self._ident_color = 0
        self._ident_blink = 0
        self._ident_until = 0.0

        # Direct/show drive state (night- and lease-gated).
        self._lease_until = 0.0
        self._last_frame_at: float | None = None
        self._target = [0, 0, 0, 0]   # rgbw the wire asked for
        self._current = [0, 0, 0, 0]  # rgbw after slew
        self._hard_cut = False

        self.gated = False  # last show/direct frame refused by the night gate
        self.drop_short = 0
        self.drop_bad_ver = 0
        self.not_mine = 0

        # Uplink bookkeeping.
        self._seq = 0
        self._boot_at = clock()
        self._next_short_at = clock()  # dev cadence: 1 Hz
        self._next_full_at = clock()   # every 60 s + on state change
        self._full_due = True          # boot heartbeat is a full one
        self._last_life_reported: int | None = None
        # Telemetry knobs tests/demos may set directly.
        self.batt_ma = -120  # discharging; set > 0 to simulate solar charging
        self.dl_rssi = -40 - (sum(self._id3) % 20)

    # ------------------------------------------------------------------
    # Lifecycle / drive state
    # ------------------------------------------------------------------

    @property
    def night(self) -> bool:
        if self._force is not None:
            return self._force == 1
        return self._auto_night

    @property
    def life_state(self) -> str:
        return "night" if self.night else "day"

    @property
    def lease(self) -> dict | None:
        now = self._clock()
        if now >= self._lease_until:
            return None
        return {"until_s": round(self._lease_until - now, 2)}

    @property
    def identify(self) -> dict | None:
        now = self._clock()
        if now >= self._ident_until:
            return None
        return {
            "color": self._ident_color,
            "blink": bool(self._ident_blink),
            "until_s": round(self._ident_until - now, 2),
        }

    @property
    def last_rx_age(self) -> float | None:
        if self._last_frame_at is None:
            return None
        return self._clock() - self._last_frame_at

    # ------------------------------------------------------------------
    # RX ladder (hand-rolled -- see module docstring)
    # ------------------------------------------------------------------

    def consume(self, raw: bytes) -> None:
        if len(raw) < 13:  # espnow_link.cpp: len < sizeof(NbHeader) -> drop
            self.drop_short += 1
            return
        if raw[0] != 1:  # NB_PROTO_VER gate
            self.drop_bad_ver += 1
            return
        ptype = raw[1]
        if ptype == _T_IDENTIFY:
            self._on_identify(raw)
        elif ptype == _T_SHOWFRAME:
            self._on_showframe(raw)
        elif ptype == _T_DIRECT_FRAME:
            self._on_direct_frame(raw)
        elif ptype == _T_PROGRAM_SET:
            self._on_program_set(raw)
        elif ptype == _T_FORCE_LIFECYCLE:
            self._on_force_lifecycle(raw)
        # Unknown types: real peers ignore them silently (append-only
        # doctrine -- old firmware must coexist with newer senders).

    def _mine(self, target: bytes) -> bool:
        if target == b"\x00\x00\x00" or target == self._id3:
            return True
        self.not_mine += 1
        return False

    def _on_identify(self, raw: bytes) -> None:
        # net_peer.cpp: len < header+4 -> drop; len >= sizeof -> color tail.
        if len(raw) < 17:
            self.drop_short += 1
            return
        if not self._mine(raw[13:16]):
            return
        secs = raw[16]
        has_color = len(raw) >= 19
        self._ident_color = raw[17] if has_color else 0
        self._ident_blink = raw[18] if has_color else 0
        # secs=0 -> until = now: cancels immediately (net_peer.cpp).
        self._ident_until = self._clock() + secs

    def _grant_lease_and_note(self, flags: int, rgbw: list[int]) -> bool:
        """Shared show/direct gate: night first, then lease. True = accepted."""
        now = self._clock()
        if not self.night:
            self.gated = True  # visible refusal -- the bench trap made loud
            return False
        if flags & 0x01:
            self._lease_until = now + _MICROLEASE_S
        elif now >= self._lease_until:
            return False  # no lease, no grant bit: runtime.cpp ignores it
        self.gated = False
        self._last_frame_at = now
        self._target = rgbw
        self._hard_cut = bool(flags & 0x02)
        return True

    def _on_showframe(self, raw: bytes) -> None:
        # 17 B legacy (val defaults 255) / 22 B full. Broadcast, no target.
        if len(raw) < 17:
            self.drop_short += 1
            return
        hue = raw[15]
        flags = raw[16]
        val = raw[17] if len(raw) >= 22 else 255
        r, g, b = _hsv_to_rgb(hue, val)
        self._grant_lease_and_note(flags & 0x01, [r, g, b, 0])

    def _on_direct_frame(self, raw: bytes) -> None:
        # Gate: at least one entry (15 + 7); count = min(count, (len-15)/7).
        if len(raw) < 15 + 7:
            self.drop_short += 1
            return
        flags = raw[13]
        count = min(raw[14], (len(raw) - 15) // 7)
        for i in range(count):
            off = 15 + 7 * i
            if raw[off : off + 3] == self._id3:
                r, g, b, w = raw[off + 3 : off + 7]
                if not self.cls.is_rgbw:
                    w = 0  # GRB hex has no W emitter
                self._grant_lease_and_note(flags, [r, g, b, w])
                return
        self.not_mine += 1  # a frame that names others is not for us

    def _on_program_set(self, raw: bytes) -> None:
        if len(raw) < 32:
            self.drop_short += 1
            return
        if not self._mine(raw[13:16]):
            return
        program_id = raw[16]
        (lease_s,) = struct.unpack_from("<H", raw, 17)
        now = self._clock()
        if program_id == 0 or lease_s == 0:
            self._lease_until = now  # release -> back to autonomous
        else:
            self._lease_until = now + lease_s
        self._note_state_change()

    def _on_force_lifecycle(self, raw: bytes) -> None:
        if len(raw) < 18:
            self.drop_short += 1
            return
        if not self._mine(raw[13:16]):
            return
        mode = raw[16]
        self._force = None if mode == 2 else mode
        if self.night:
            self.gated = False
        self._note_state_change()

    # ------------------------------------------------------------------
    # Render (called at 10 Hz by the runner, like fixture.ino renderTick)
    # ------------------------------------------------------------------

    def tick(self, now: float | None = None) -> None:
        now = self._clock() if now is None else now
        driven = (
            self.night
            and self._last_frame_at is not None
            and now < self._lease_until
        )
        if driven:
            age = now - self._last_frame_at
            if age <= _HOLD_S:
                target = self._target
            elif age <= _STALE_S:
                # hold + half: prog_bridge.cpp's loss behavior, never blank
                target = [v // 2 for v in self._target]
            else:
                driven = False
        if driven:
            if self._hard_cut:
                self._current = list(target)
            else:
                self._current = [
                    _clamp_step(c, t, _SLEW_STEP)
                    for c, t in zip(self._current, target)
                ]
        else:
            # Autonomous: slow amber breathing (~0.2 Hz) so silence is SEEN.
            level = 0.5 + 0.5 * math.sin(2 * math.pi * 0.2 * (now - self._boot_at))
            amber = (int(255 * level * 0.6), int(160 * level * 0.6), int(20 * level * 0.6))
            self._current = [*amber, 0]

    def pixels(self) -> list[RGBW]:
        """What the LEDs show right now (identify > driven/autonomous)."""
        now = self._clock()
        n = self.cls.pixel_count
        if now < self._ident_until and self._ident_color in _IDENT_COLORS:
            if self._ident_blink and int(now * 2) % 2:  # 1 Hz blink, 50% duty
                return [RGBW(0, 0, 0, 0)] * n
            r, g, b = _IDENT_COLORS[self._ident_color]
            w = 255 if (r == g == b == 255 and self.cls.is_rgbw) else 0
            return [RGBW(r, g, b, w)] * n
        r, g, b, w = (max(0, min(255, v)) for v in self._current)
        return [RGBW(r, g, b, w)] * n  # PERIMETER = uniform wash (v1)

    # ------------------------------------------------------------------
    # Uplink: real NbHeartbeat bytes, hand-packed
    # ------------------------------------------------------------------

    def _note_state_change(self) -> None:
        self._full_due = True

    def next_uplink(self, now: float | None = None) -> list[bytes]:
        """Heartbeats due at `now`: hb-short 1 Hz (dev), hb-full 60 s + edges."""
        now = self._clock() if now is None else now
        out: list[bytes] = []
        life = 3 if self.night else 1
        if life != self._last_life_reported:
            self._full_due = True
        if self._full_due or now >= self._next_full_at:
            out.append(self._heartbeat(now, full=True))
            self._full_due = False
            self._last_life_reported = life
            self._next_full_at = now + 60.0
            self._next_short_at = now + 1.0
        elif now >= self._next_short_at:
            out.append(self._heartbeat(now, full=False))
            self._next_short_at = now + 1.0
        return out

    def _heartbeat(self, now: float, *, full: bool) -> bytes:
        self._seq += 1
        buf = bytearray(_HB_FULL_LEN if full else _HB_SHORT_LEN)
        # NbHeader: ver, type=NB_HEARTBEAT(1), src_id, seq, uptime_ms
        struct.pack_into(
            "<BB3sII", buf, 0, 1, 1, self._id3, self._seq,
            int((now - self._boot_at) * 1000) & 0xFFFFFFFF,
        )
        # Base block: plausible drifting battery + fixed link quality.
        batt_mv = 3300 + int(60 * math.sin(now / 97.0 + sum(self._id3)))
        struct.pack_into("<hh", buf, _HB_OFF_BATT_MV, batt_mv, self.batt_ma)
        buf[_HB_OFF_SOC] = 80
        buf[_HB_OFF_MODE] = 0
        struct.pack_into("<b", buf, _HB_OFF_DL_RSSI, self.dl_rssi)
        if full:
            # Only the fields cambium's FleetState reads are non-zero; the
            # remaining tails stay zeroed (valid absent sentinels).
            struct.pack_into("<24s", buf, _HB_OFF_FW_REV, b"fake-fleet-0.1")
            buf[_HB_OFF_LIFE_STATE] = 3 if self.night else 1
            buf[_HB_OFF_POWER_TIER] = 0
        return bytes(buf)


def _hsv_to_rgb(hue8: int, val8: int) -> tuple[int, int, int]:
    """hue/val (0-255, full saturation) -> rgb, matching hueToRgb's intent."""
    h = (hue8 / 255.0) * 6.0
    i = int(h) % 6
    f = h - int(h)
    v = val8
    p, q, t = 0, int(v * (1 - f)), int(v * f)
    return [
        (v, t, p), (q, v, p), (p, v, t), (p, q, v), (t, p, v), (v, p, q)
    ][i]
