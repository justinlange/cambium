/* Host-side parity harness for cobs.h against the shared golden vectors.
 *
 * JSON-free on purpose: tests/test_c_cobs_parity.py generates
 * cobs_vectors_gen.h (C arrays) from tests/golden/cobs_vectors.json into a
 * temp dir, compiles this file with plain cc, and asserts exit 0. Any
 * mismatch between C cobs.h and Python cobs.py shows up here.
 */
#ifndef ARDUINO /* host-only: arduino-cli compiles every .c in the sketch dir */

#include <stdio.h>
#include <string.h>

#include "cobs.h"
#include "cobs_vectors_gen.h" /* generated: cobs_vec_t cobs_vectors[], cobs_vector_count */

int main(void) {
  int failures = 0;
  for (unsigned v = 0; v < cobs_vector_count; v++) {
    const cobs_vec_t *t = &cobs_vectors[v];
    uint8_t buf[600];
    size_t n;

    /* encode(decoded) must equal the golden encoded bytes */
    n = cobs_encode(t->decoded, t->decoded_len, buf);
    if (n != t->encoded_len || memcmp(buf, t->encoded, n) != 0) {
      fprintf(stderr, "FAIL %s: encode mismatch (got %zu bytes, want %u)\n",
              t->name, n, t->encoded_len);
      failures++;
    }

    /* decode(encoded) must equal the golden decoded bytes */
    if (cobs_decode(t->encoded, t->encoded_len, buf, &n) != 0 ||
        n != t->decoded_len || memcmp(buf, t->decoded, n) != 0) {
      fprintf(stderr, "FAIL %s: decode mismatch\n", t->name);
      failures++;
    }

    /* crc16_ccitt over the DECODED bytes must equal the golden crc */
    if (crc16_ccitt(t->decoded, t->decoded_len) != t->crc16) {
      fprintf(stderr, "FAIL %s: crc 0x%04x != golden 0x%04x\n", t->name,
              crc16_ccitt(t->decoded, t->decoded_len), t->crc16);
      failures++;
    }
  }

  /* standard CRC-16/CCITT-FALSE check value pins the algorithm itself */
  if (crc16_ccitt((const uint8_t *)"123456789", 9) != 0x29B1) {
    fprintf(stderr, "FAIL crc16 check value != 0x29B1\n");
    failures++;
  }

  if (failures) {
    fprintf(stderr, "%d parity failure(s)\n", failures);
    return 1;
  }
  printf("all %u vectors ok\n", cobs_vector_count);
  return 0;
}

#endif /* !ARDUINO */
