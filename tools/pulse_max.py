"""Fleet pulse experiment: all lanterns full RGBW 0.5 s ON / 5 s OFF, hard-cut.

One NbDirectFrame per transition carries every fixture (flags bit0 = micro-
lease, bit1 = hard-cut/no slew), so the whole fleet switches simultaneously.
The per-fixture PowerBudget cap still governs actual output (doctrine).

Usage (daemon must NOT hold the port):
    .venv/bin/python tools/pulse_max.py --port /dev/cu.usbmodem1401 --minutes 3
"""
from __future__ import annotations

import argparse
import sys
import time

sys.path.insert(0, ".")

import serial

from cambium.roster import Roster
from cambium.wire.framing import FTYPE_RADIO_TX, encode_frame
from cambium.wire.packets import NbHeader, direct_frame, short_id_from_str

FLAGS_LEASE_HARDCUT = 0b11


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", required=True)
    ap.add_argument("--roster", default="config/roster-onsite.csv")
    ap.add_argument("--minutes", type=float, default=3.0)
    ap.add_argument("--on-s", type=float, default=0.5)
    ap.add_argument("--off-s", type=float, default=5.0)
    args = ap.parse_args()

    roster = Roster.load(args.roster)
    macs = [short_id_from_str(f.mac) for f in roster.fixtures]
    on_entries = [(m, 255, 255, 255, 255) for m in macs]
    off_entries = [(m, 0, 0, 0, 0) for m in macs]

    s = serial.Serial(args.port, 115200, timeout=1)
    seq = 5000
    try:
        time.sleep(1.5)  # port open DTR-reboots the bridge
        end = time.time() + args.minutes * 60
        pulses = 0
        while time.time() < end:
            for entries, hold in ((on_entries, args.on_s), (off_entries, args.off_s)):
                h = NbHeader(src_id=b"\xca\x4d\x00", seq=seq)
                seq += 1
                # chunks of <=18 fit one packet; roster may grow past that
                for i in range(0, len(entries), 18):
                    s.write(encode_frame(
                        FTYPE_RADIO_TX,
                        direct_frame(h, entries[i : i + 18], flags=FLAGS_LEASE_HARDCUT),
                    ))
                s.flush()
                time.sleep(hold)
            pulses += 1
        print(f"done: {pulses} pulses")
    finally:
        s.close()


if __name__ == "__main__":
    main()
