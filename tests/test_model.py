import pytest

from cambium.model import RGBW, Fixture, FixtureClass, FixtureFrame, TelemetryUpdate


def test_fixture_class_values_mirror_firmware():
    # Pinned to fixture_context.h -- wire/NVS-stable, must never drift.
    assert FixtureClass.UNKNOWN == 0
    assert FixtureClass.DOWNLIGHT == 1
    assert FixtureClass.PERIMETER == 2
    assert FixtureClass.UPLIGHT == 3
    assert FixtureClass.CHANDELIER == 4
    assert len(FixtureClass) == 5


def test_pixel_count():
    assert FixtureClass.PERIMETER.pixel_count == 37
    for c in (FixtureClass.UNKNOWN, FixtureClass.DOWNLIGHT,
              FixtureClass.UPLIGHT, FixtureClass.CHANDELIER):
        assert c.pixel_count == 1


def test_is_rgbw():
    assert FixtureClass.PERIMETER.is_rgbw is False
    for c in (FixtureClass.UNKNOWN, FixtureClass.DOWNLIGHT,
              FixtureClass.UPLIGHT, FixtureClass.CHANDELIER):
        assert c.is_rgbw is True


def test_rgbw_valid():
    c = RGBW(0, 128, 255)
    assert (c.r, c.g, c.b, c.w) == (0, 128, 255, 0)
    assert RGBW(1, 2, 3, 4).w == 4


@pytest.mark.parametrize("kwargs,channel", [
    (dict(r=-1, g=0, b=0, w=0), "r"),
    (dict(r=0, g=256, b=0, w=0), "g"),
    (dict(r=0, g=0, b=1.5, w=0), "b"),
    (dict(r=0, g=0, b=0, w=999), "w"),
])
def test_rgbw_invalid_message_contains_fix(kwargs, channel):
    with pytest.raises(ValueError) as e:
        RGBW(**kwargs)
    msg = str(e.value)
    assert f"RGBW.{channel}" in msg
    assert "0..255" in msg
    assert "clamp" in msg  # the fix


def test_fixture_happy():
    f = Fixture("9E5AE8", "B003", FixtureClass.PERIMETER, (1.0, 2.0, 3.0))
    assert f.mac == "9E5AE8"
    g = Fixture("F2BDB4", None, FixtureClass.DOWNLIGHT, None)
    assert g.fixture_id is None and g.xyz is None


@pytest.mark.parametrize("bad", ["9e5ae8", "68:EE:8F:F2:BD:B4", "F2BD", "GGGGGG", ""])
def test_fixture_bad_mac_message_contains_fix(bad):
    with pytest.raises(ValueError) as e:
        Fixture(bad, None, FixtureClass.DOWNLIGHT, None)
    msg = str(e.value)
    assert "6 uppercase hex digits" in msg
    assert "Roster.load()" in msg  # the fix


def test_fixture_frame():
    fr = FixtureFrame(seq=7, colors={"F2BDB4": RGBW(1, 2, 3, 4)})
    assert fr.seq == 7
    assert fr.colors["F2BDB4"].b == 3
    assert FixtureFrame(seq=0).colors == {}


def test_telemetry_update_hb_full_tails_optional():
    t = TelemetryUpdate(mac="F2BDB4", batt_mv=3300, batt_ma=-120, soc_pct=87,
                        dl_rssi=-61, mode=2, last_seen=123.5)
    assert t.life_state is None and t.program is None and t.power_tier is None
    t2 = TelemetryUpdate(mac="F2BDB4", batt_mv=3300, batt_ma=-120, soc_pct=87,
                         dl_rssi=-61, mode=2, last_seen=124.0,
                         life_state=3, program=1, power_tier=0)
    assert (t2.life_state, t2.program, t2.power_tier) == (3, 1, 0)
