from pathlib import Path

import pytest

from cambium.config import CambiumConfig

SHIPPED_TOML = Path(__file__).parent.parent / "config" / "cambium.toml"


def test_defaults():
    c = CambiumConfig()
    assert c.host == "0.0.0.0"
    assert c.port == 8600
    assert c.channel == 11
    assert c.tx_hz == 8.0
    assert c.stale_input_s == 2.0
    assert c.white_extract == "subtract"
    assert c.site == "bench"
    assert c.roster == "config/roster-bench10.csv"


def test_shipped_toml_matches_defaults():
    assert CambiumConfig.load(SHIPPED_TOML) == CambiumConfig()


def test_load_overrides(tmp_path):
    p = tmp_path / "c.toml"
    p.write_text(
        '[server]\nport = 9000\n'
        '[radio]\nchannel = 6\ntx_hz = 4.0\n'
        '[paths]\nsite = "playa"\nroster = "config/roster-playa.csv"\n'
    )
    c = CambiumConfig.load(p)
    assert c.port == 9000
    assert c.channel == 6
    assert c.tx_hz == 4.0
    assert c.site == "playa"
    assert c.roster == "config/roster-playa.csv"
    # untouched keys keep defaults
    assert c.host == "0.0.0.0"
    assert c.stale_input_s == 2.0
    assert c.white_extract == "subtract"


def test_partial_file_keeps_defaults(tmp_path):
    p = tmp_path / "c.toml"
    p.write_text("[server]\nport = 8700\n")
    c = CambiumConfig.load(p)
    assert c.port == 8700
    assert c.channel == 11


def test_unknown_key_error_names_key_and_lists_valid(tmp_path):
    p = tmp_path / "c.toml"
    p.write_text("[radio]\ntx_hx = 8.0\n")
    with pytest.raises(ValueError) as e:
        CambiumConfig.load(p)
    msg = str(e.value)
    assert str(p) in msg
    assert "tx_hx" in msg and "[radio]" in msg
    assert "channel" in msg and "tx_hz" in msg and "stale_input_s" in msg
    assert "remove it or fix the spelling" in msg  # the fix


def test_unknown_section_error(tmp_path):
    p = tmp_path / "c.toml"
    p.write_text("[radioo]\nchannel = 11\n")
    with pytest.raises(ValueError) as e:
        CambiumConfig.load(p)
    msg = str(e.value)
    assert "[radioo]" in msg
    assert "[server]" in msg and "[radio]" in msg and "[color]" in msg and "[paths]" in msg


def test_top_level_key_error(tmp_path):
    p = tmp_path / "c.toml"
    p.write_text("port = 8600\n")
    with pytest.raises(ValueError) as e:
        CambiumConfig.load(p)
    assert "port" in str(e.value)
