# cambium_bridge

Dumb radio modem: relays COBS-framed USB serial <-> ESP-NOW broadcast for the
cambium daemon. It knows nothing about Nb packet internals, so packet.h can
evolve forever without reflashing this board. Serial contract ground truth:
`cambium/wire/framing.py` + `cobs.py`; C/Python parity is enforced by
`tests/test_c_cobs_parity.py` against `tests/golden/cobs_vectors.json`.

## Flash

The earmarked board is **F2BED4** (registry role `serial_bridge`), a
PowerFeather V2 / ESP32-S3 on USB power:

    ./build.sh --port /dev/cu.usbmodemXXXX

## Channel discipline

The bridge must sit on the fleet's ESP-NOW channel (**11**) or it hears
nothing. `--channel N` changes the build default (`-DCB_CHANNEL=N`);
`CTRL_SET_CHANNEL` retunes at runtime (RAM only -- reboot returns to the
build default).

## esp32 core version

`esp32s3_powerfeather` needs esp32 core >= 3.x. On core 2.x (this machine has
2.0.7) build.sh explains the fix: `arduino-cli core upgrade esp32:esp32` or
`--fqbn esp32:esp32:esp32s3` (generic S3 -- the sketch has no board-specific
code, and version guards cover both cores' ESP-NOW callback signatures).
