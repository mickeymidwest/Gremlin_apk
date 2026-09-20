/* Minimal netstring reader.  A netstring is  <len> ":" <data> ","
 * e.g.  "5:hello,".  See http://cr.yp.to/proto/netstrings.txt
 *
 * This is deliberately a small, self-contained parser with (at least one)
 * planted memory-safety bug for fuzzing practice.  Do not "fix" it -- the
 * job is to write a harness that makes libFuzzer/ASAN find the bug.
 */
#ifndef NETSTRING_H
#define NETSTRING_H

#include <stddef.h>

#ifdef __cplusplus
extern "C" {
#endif

typedef enum {
    NS_OK = 0,
    NS_ERR_FORMAT = -1,   /* not a well-formed netstring            */
    NS_ERR_LENGTH = -2,    /* declared length overflows / too large  */
    NS_ERR_TRUNC  = -3     /* input ended before the netstring did   */
} ns_status;

/* Parse ONE netstring from `in` (of `in_len` bytes).
 * On success, writes the payload into `out` (a caller buffer of `out_cap`
 * bytes), sets *out_len to the payload length, and *consumed to the number
 * of input bytes used.  Returns NS_OK or an ns_status error. */
ns_status ns_parse(const unsigned char *in, size_t in_len,
                   unsigned char *out, size_t out_cap,
                   size_t *out_len, size_t *consumed);

#ifdef __cplusplus
}
#endif

#endif
