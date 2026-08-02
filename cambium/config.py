"""Cambium runtime configuration, loaded from a TOML file.

Unknown keys are hard errors on purpose: a typo like `tx_hx` silently
falling back to a default would be miserable to debug on site.
"""

import tomllib
from dataclasses import dataclass
from pathlib import Path

# TOML section -> allowed keys. Key names match CambiumConfig field names
# exactly so load() can setattr() without a mapping table.
_VALID_KEYS = {
    "server": ("host", "port"),
    "radio": ("channel", "tx_hz", "stale_input_s"),
    "color": ("white_extract",),
    "paths": ("site", "roster"),
}


@dataclass
class CambiumConfig:
    # [server]
    host: str = "0.0.0.0"
    port: int = 8600
    # [radio]
    channel: int = 11
    tx_hz: float = 8.0
    stale_input_s: float = 2.0
    # [color]
    white_extract: str = "subtract"
    # [paths]
    site: str = "bench"
    roster: str = "config/roster-bench10.csv"

    @classmethod
    def load(cls, path: str | Path) -> "CambiumConfig":
        p = Path(path)
        with p.open("rb") as f:
            data = tomllib.load(f)
        cfg = cls()
        for section, table in data.items():
            if section not in _VALID_KEYS:
                raise ValueError(
                    f"{p}: unknown section [{section}]; valid sections: "
                    + ", ".join(f"[{s}]" for s in _VALID_KEYS)
                    + " -- remove it or fix the spelling"
                )
            if not isinstance(table, dict):
                raise ValueError(
                    f"{p}: top-level key {section!r} is not allowed; move it "
                    f"under one of " + ", ".join(f"[{s}]" for s in _VALID_KEYS)
                )
            for key, value in table.items():
                if key not in _VALID_KEYS[section]:
                    raise ValueError(
                        f"{p}: unknown key '{key}' in [{section}]; valid keys "
                        f"in [{section}]: " + ", ".join(_VALID_KEYS[section])
                        + " -- remove it or fix the spelling"
                    )
                setattr(cfg, key, value)
        return cfg
