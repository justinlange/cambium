"""Send NB_TARGET_ENTER_MAINT / NB_ENTER_MAINT / NB_RESUME via the cambium bridge.

Usage (the daemon must NOT hold the port):
    .venv/bin/python tools/send_enter_maint.py --port /dev/cu.usbmodem1401 --target F2BF54
    .venv/bin/python tools/send_enter_maint.py --port /dev/cu.usbmodem1401 --resume

Old fleet firmware understands all three (types 3/4/16 are original protocol).
A lantern below its battery floor and without solar refuses and stays in comms.
"""
from __future__ import annotations

import argparse
import sys
import time

sys.path.insert(0, ".")

import serial  # pyserial

from cambium.wire.framing import FTYPE_RADIO_TX, encode_frame
from cambium.wire.packets import (
    NbHeader,
    NbType,
    enter_maint,
    resume,
    short_id_from_str,
    _header_bytes,
)


def target_enter_maint(h: NbHeader, target: bytes) -> bytes:
    # NbTargetCmd: header + target_id[3] + arg (17 B), arg unused -> 0
    return _header_bytes(h, NbType.TARGET_ENTER_MAINT) + target + b"\x00"


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", required=True)
    ap.add_argument("--target", help="short mac like F2BF54; omit for broadcast")
    ap.add_argument("--resume", action="store_true", help="send NB_RESUME instead")
    ap.add_argument("--repeat", type=int, default=5, help="redundant sends (unacked radio)")
    args = ap.parse_args()

    target = short_id_from_str(args.target) if args.target else None

    s = serial.Serial(args.port, 115200, timeout=1)
    try:
        time.sleep(1.5)  # opening the port DTR-reboots the bridge; let it come up
        for n in range(args.repeat):
            h = NbHeader(src_id=b"\xca\x4d\x00", seq=1000 + n)
            if args.resume:
                pkt, what = resume(h), "RESUME (all)"
            elif target is not None:
                pkt, what = target_enter_maint(h, target), f"TARGET_ENTER_MAINT -> {args.target}"
            else:
                pkt, what = enter_maint(h), "ENTER_MAINT (all)"
            s.write(encode_frame(FTYPE_RADIO_TX, pkt))
            s.flush()
            print(f"sent {what} ({n + 1}/{args.repeat})")
            time.sleep(0.5)
    finally:
        s.close()


if __name__ == "__main__":
    main()
