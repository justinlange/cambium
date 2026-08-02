/* COBS (Consistent Overhead Byte Stuffing) + CRC-16/CCITT-FALSE.
 *
 * Header-only, no Arduino dependencies -- compiles on host cc for the parity
 * test (tests/test_c_cobs_parity.py) and in the cambium_bridge sketch.
 *
 * Algorithm is byte-for-byte identical to cambium/wire/cobs.py (the serial
 * contract ground truth), including the canonical boundary case: a maximal
 * 254-byte non-zero block at end of input gets NO phantom trailing group.
 * Both sides are held to tests/golden/cobs_vectors.json.
 *
 * crc16_ccitt is CRC-16/CCITT-FALSE: poly 0x1021, init 0xFFFF, no reflection,
 * no final XOR. Check value: crc16_ccitt("123456789", 9) == 0x29B1.
 */
#ifndef CAMBIUM_BRIDGE_COBS_H
#define CAMBIUM_BRIDGE_COBS_H

#include <stddef.h>
#include <stdint.h>

/* Worst-case encoded size for n input bytes (one code byte per started
 * 254-block, plus one for the empty-input/trailing-zero group). */
#define COBS_ENCODE_MAX(n) ((n) + (n) / 254 + 1)

/* Encode n bytes from `in` into `out`; returns encoded length.
 * `out` must hold COBS_ENCODE_MAX(n) bytes. Output contains no 0x00.
 * Empty input encodes to the single byte 0x01. */
static inline size_t cobs_encode(const uint8_t *in, size_t n, uint8_t *out) {
  size_t i = 0, o = 0;
  for (;;) {
    /* A block is up to 254 non-zero bytes, terminated by a zero (consumed)
     * or by the 254-byte cap (code 0xFF = "no zero after this block"). */
    size_t limit = i + 254;
    if (limit > n) limit = n;
    size_t zero_at = limit; /* sentinel: no zero in [i, limit) */
    for (size_t j = i; j < limit; j++) {
      if (in[j] == 0) { zero_at = j; break; }
    }
    if (zero_at == limit) {
      /* no zero in the window: block runs to the cap (or end of input) */
      size_t blen = limit - i;
      out[o++] = (uint8_t)(blen + 1);
      for (size_t j = 0; j < blen; j++) out[o++] = in[i + j];
      i = limit;
      if (i >= n) break;
    } else {
      size_t blen = zero_at - i;
      out[o++] = (uint8_t)(blen + 1);
      for (size_t j = 0; j < blen; j++) out[o++] = in[i + j];
      i = zero_at + 1; /* consume the zero */
      if (i >= n) {
        /* Input ended ON a zero: emit the empty final block for it. */
        out[o++] = 0x01;
        break;
      }
    }
  }
  return o;
}

/* Decode one COBS-encoded chunk (no 0x00 delimiter included) into `out`,
 * which must hold at least n bytes. Returns 0 and sets *out_len on success;
 * returns -1 on malformed input (empty chunk, embedded zero, truncated
 * block) -- the framing layer drops the frame and resyncs on the next
 * delimiter. */
static inline int cobs_decode(const uint8_t *in, size_t n, uint8_t *out,
                              size_t *out_len) {
  size_t i = 0, o = 0;
  if (n == 0) return -1;
  while (i < n) {
    uint8_t code = in[i];
    if (code == 0) return -1; /* unstripped delimiter */
    i++;
    size_t end = i + (size_t)code - 1;
    if (end > n) return -1; /* truncated block */
    for (; i < end; i++) {
      if (in[i] == 0) return -1; /* zero inside a block */
      out[o++] = in[i];
    }
    if (code != 0xFF && i < n) out[o++] = 0;
  }
  *out_len = o;
  return 0;
}

/* CRC-16/CCITT-FALSE (poly 0x1021, init 0xFFFF, no reflect/xorout). */
static inline uint16_t crc16_ccitt(const uint8_t *data, size_t len) {
  uint16_t crc = 0xFFFF;
  for (size_t i = 0; i < len; i++) {
    crc ^= (uint16_t)data[i] << 8;
    for (int b = 0; b < 8; b++) {
      if (crc & 0x8000) crc = (uint16_t)((crc << 1) ^ 0x1021);
      else crc = (uint16_t)(crc << 1);
    }
  }
  return crc;
}

#endif /* CAMBIUM_BRIDGE_COBS_H */
