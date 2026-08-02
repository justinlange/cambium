"""Color policy: sim linear floats -> canonical 8-bit RGBW.

NO gamma here. The firmware applies resGamma8 at render time, so cambium
sends linear 8-bit values; gamma-correcting twice would crush the low end.
Display GAIN in the simulator is view-only (it brightens the operator's
screen, not the tree) and never reaches us.

Sim colors are linear floats that may exceed 1.0 (the renderer accumulates
light additively); the policy is a hard clamp to [0, 1] before quantizing.
"""

from cambium.model import FixtureClass, RGBW

# Valid [color] white_extract values in cambium.toml.
POLICIES = ("subtract", "none")


def clamp01(v: float) -> float:
    """Hard-clamp a linear sim channel into [0.0, 1.0]."""
    if v < 0.0:
        return 0.0
    if v > 1.0:
        return 1.0
    return v


def quantize8(v: float) -> int:
    """Linear float channel -> 0..255 int (clamped, rounded)."""
    return round(clamp01(v) * 255)


def white_extract(r8: int, g8: int, b8: int, cls: FixtureClass, policy: str) -> RGBW:
    """Map 8-bit RGB onto the fixture's emitters per the configured policy.

    "subtract" (the default): for RGBW hardware, move the achromatic part of
    the color onto the white channel -- w = min(r, g, b), then subtract w from
    each of r/g/b. This preserves hue exactly and routes the gray component
    through the W emitter, which is more efficient (lumens/W) than mixing
    white from R+G+B.

    PERIMETER is GRB-only (no white emitter, see FixtureClass.is_rgbw): any w
    we sent would be ignored bytes at best, so w stays 0 and rgb is untouched
    regardless of policy.

    "none": w = 0 always, rgb untouched -- for A/B-ing the extraction on site.
    """
    if policy not in POLICIES:
        raise ValueError(
            f"unknown white_extract policy {policy!r}; set [color] white_extract "
            f"in cambium.toml to one of: " + ", ".join(POLICIES)
        )
    if policy == "subtract" and cls.is_rgbw:
        w = min(r8, g8, b8)
        return RGBW(r8 - w, g8 - w, b8 - w, w)
    return RGBW(r8, g8, b8, 0)
