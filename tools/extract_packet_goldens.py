#!/usr/bin/env python3
"""Extract golden sizeof/offsetof pins from the firmware layout test.

Reads resonance-hardware/firmware/fixture/tests/test_packet_layout.cpp (the
fleet's build-time wire-format tripwire) and writes the numeric CHECK_EQ pins
to tests/golden/packet_pins.json so tests/test_packet_parity.py can hold
cambium's Python layouts to the exact same numbers.

The parser is deliberately dumb: one regex per line. CHECK_EQ lines whose
right-hand side is a plain unsigned literal (or a sum of them, e.g.
"13u + 3u + 1u") become pins keyed by their normalized left-hand expression.
Anything else -- CHECK() truth gates, non-numeric RHS -- is skipped silently
but counted, so a growing skip count flags that the firmware test gained
shapes this script doesn't understand.

Usage:  python tools/extract_packet_goldens.py [path-to-cpp] [output-json]
"""

from __future__ import annotations

import hashlib
import json
import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CPP = (
    REPO_ROOT.parent
    / "resonance-hardware/firmware/fixture/tests/test_packet_layout.cpp"
)
DEFAULT_OUT = REPO_ROOT / "tests/golden/packet_pins.json"

# CHECK_EQ(<lhs>, <rhs>);  where rhs is unsigned literals joined by '+'
_CHECK_EQ = re.compile(
    r"CHECK_EQ\(\s*(?P<lhs>.+?)\s*,\s*(?P<rhs>\d+u(?:\s*\+\s*\d+u)*)\s*\)\s*;"
)
# Any CHECK/CHECK_EQ-looking line, to know what we skipped.
_ANY_CHECK = re.compile(r"^\s*CHECK(_EQ)?\(")


def _normalize_lhs(lhs: str) -> str:
    # "(unsigned)NB_HB_SHORT_LEN" -> "NB_HB_SHORT_LEN"; collapse whitespace so
    # "offsetof(NbHeartbeat, supply_mv)" keys are stable.
    lhs = re.sub(r"^\(\s*unsigned\s*\)\s*", "", lhs)
    return re.sub(r"\s+", " ", lhs).strip()


def _eval_rhs(rhs: str) -> int:
    return sum(int(term.strip().rstrip("uU")) for term in rhs.split("+"))


def extract(cpp_path: Path, out_path: Path) -> dict:
    text = cpp_path.read_text()
    pins: dict[str, int] = {}
    skipped = 0
    for line in text.splitlines():
        m = _CHECK_EQ.search(line)
        if m:
            pins[_normalize_lhs(m.group("lhs"))] = _eval_rhs(m.group("rhs"))
        elif _ANY_CHECK.search(line):
            skipped += 1
    result = {
        "source": cpp_path.name,
        "source_sha256": hashlib.sha256(text.encode()).hexdigest(),
        "skipped_check_lines": skipped,
        "pins": pins,
    }
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(result, indent=2) + "\n")
    return result


def main() -> None:
    cpp = Path(sys.argv[1]) if len(sys.argv) > 1 else DEFAULT_CPP
    out = Path(sys.argv[2]) if len(sys.argv) > 2 else DEFAULT_OUT
    if not cpp.exists():
        sys.exit(
            f"layout test not found at {cpp}; pass its path as argv[1] "
            f"(it lives in Ben's resonance-hardware repo)"
        )
    result = extract(cpp, out)
    print(
        f"wrote {len(result['pins'])} pins "
        f"({result['skipped_check_lines']} CHECK lines skipped) to {out}"
    )


if __name__ == "__main__":
    main()
