"""SimFrame -> FixtureFrame: the fixture_id -> MAC join plus color policy.

This is the only place the browser's view of the fleet (fixture ids, linear
floats) is translated into the canonical form (short macs, 8-bit RGBW).
"""

from cambium.model import FixtureFrame, RGBW
from cambium.normalize.color import quantize8, white_extract
from cambium.roster import Roster

_HEX = set("0123456789ABCDEF")


def sim_to_canonical(
    sim_fixtures: list[dict],
    roster: Roster,
    policy: str,
    seq: int,
) -> tuple[FixtureFrame, list[str]]:
    """Normalize one sim frame. Returns (frame, unknown_ids).

    sim_fixtures entries look like {"id": "B003", "rgb": [r, g, b]} with
    linear float channels. "id" is a roster fixture_id, or -- forgiving
    input -- a raw 6-hex short mac (any case): during mapping, fixtures have
    macs before they have human labels. fixture_id wins if both match.

    Unknown ids are collected and returned, never raised: the browser's scene
    may be ahead of (or behind) the roster on disk, and one unmapped fixture
    must not black out the rest of the tree. The caller decides whether to
    log or surface them.

    seq is supplied by the caller (the api layer owns frame ordering).
    """
    colors: dict[str, RGBW] = {}
    unknown: list[str] = []
    for entry in sim_fixtures:
        fid = entry["id"]
        fixture = roster.by_id.get(fid)
        if fixture is None:
            m = fid.strip().upper()
            if len(m) == 6 and all(c in _HEX for c in m):
                fixture = roster.by_mac.get(m)
        if fixture is None:
            unknown.append(fid)
            continue
        r, g, b = entry["rgb"]
        colors[fixture.mac] = white_extract(
            quantize8(r), quantize8(g), quantize8(b), fixture.cls, policy
        )
    return FixtureFrame(seq=seq, colors=colors), unknown
