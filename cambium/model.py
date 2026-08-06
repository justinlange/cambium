"""Pure data model shared across cambium. No I/O here.

FixtureClass mirrors firmware/fixture/src/core/fixture_context.h exactly:
the values are wire/NVS-stable in Ben's firmware, so they must never drift.
"""

from dataclasses import dataclass, field
from enum import IntEnum


class FixtureClass(IntEnum):
    # Values pinned to fixture_context.h (ADR 0024/0027) -- do not renumber.
    UNKNOWN = 0
    DOWNLIGHT = 1  # 1 px 4 W RGBW + gobo
    PERIMETER = 2  # 37 px SK6812 HEX (GRB, no white channel)
    UPLIGHT = 3    # legacy firmware/NVS name for the 1 px trunk-light class
    CHANDELIER = 4 # 1 px RGBW safe default

    @property
    def pixel_count(self) -> int:
        return 37 if self is FixtureClass.PERIMETER else 1

    @property
    def is_rgbw(self) -> bool:
        # PERIMETER hex modules are GRB-only; W bytes sent to them are ignored.
        return self is not FixtureClass.PERIMETER


@dataclass(frozen=True)
class RGBW:
    r: int
    g: int
    b: int
    w: int = 0

    def __post_init__(self) -> None:
        for name in ("r", "g", "b", "w"):
            v = getattr(self, name)
            if not isinstance(v, int) or isinstance(v, bool) or not 0 <= v <= 255:
                raise ValueError(
                    f"RGBW.{name}={v!r} is not an int in 0..255; "
                    f"clamp or round the channel before building RGBW"
                )


@dataclass
class Fixture:
    mac: str  # short id: last 3 MAC bytes as 6 uppercase hex digits, e.g. "9E5AE8"
    fixture_id: str | None  # human label like "B003"/"F012", or None if unassigned
    cls: FixtureClass
    xyz: tuple[float, float, float] | None  # site coordinates, None until surveyed

    def __post_init__(self) -> None:
        if len(self.mac) != 6 or self.mac != self.mac.upper() or any(
            c not in "0123456789ABCDEF" for c in self.mac
        ):
            raise ValueError(
                f"Fixture.mac={self.mac!r} must be 6 uppercase hex digits like '9E5AE8'; "
                f"load fixtures through Roster.load() which normalizes full MACs"
            )


@dataclass
class FixtureFrame:
    """One rendered fleet frame: per-fixture colors keyed by short mac."""

    seq: int
    colors: dict[str, RGBW] = field(default_factory=dict)


@dataclass
class TelemetryUpdate:
    """Latest heartbeat state for one fixture, normalized from NbHeartbeat.

    life_state/program/power_tier only arrive in hb-full tails (tail 13),
    so they stay None until the first full heartbeat is seen.
    """

    mac: str
    batt_mv: int
    batt_ma: int
    soc_pct: int
    dl_rssi: int
    mode: int
    last_seen: float  # time.monotonic() at receipt -- for staleness, not wall time
    life_state: int | None = None
    program: int | None = None
    power_tier: int | None = None
