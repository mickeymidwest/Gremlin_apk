#include "netstring.h"

/* Parse the leading run of ASCII digits as a length.
 * Returns the number of digit bytes consumed, or 0 if the first byte
 * is not a digit.  Writes the value through *val. */
static size_t parse_len(const unsigned char *p, size_t n, size_t *val)
{
    size_t v = 0, i = 0;
    for (; i < n && p[i] >= '0' && p[i] <= '9'; i++)
        v = v * 10 + (size_t)(p[i] - '0');
    *val = v;
    return i;
}

/* Some callers want the payload upper-cased in place first.  We copy it
 * into a small scratch buffer, fold case, then hand it back.  This is the
 * planted bug: `scratch` is 32 bytes but the copy trusts `len`. */
static void normalize_into_scratch(const unsigned char *payload, size_t len,
                                   unsigned char *dst)
{
    unsigned char scratch[32];
    for (size_t i = 0; i < len; i++) {
        unsigned char c = payload[i];
        if (c >= 'a' && c <= 'z')
            c = (unsigned char)(c - 32);
        scratch[i] = c;                 /* BUG: no `i < sizeof scratch` guard */
    }
    for (size_t i = 0; i < len; i++)
        dst[i] = scratch[i];
}

ns_status ns_parse(const unsigned char *in, size_t in_len,
                   unsigned char *out, size_t out_cap,
                   size_t *out_len, size_t *consumed)
{
    size_t len = 0;
    size_t d = parse_len(in, in_len, &len);
    if (d == 0)
        return NS_ERR_FORMAT;              /* need at least one digit */

    if (len > (size_t)16 * 1024 * 1024)
        return NS_ERR_LENGTH;

    /* Layout after the digits:  ':' <len bytes> ',' */
    if (d + 1 > in_len || in[d] != ':')
        return NS_ERR_FORMAT;

    const unsigned char *payload = in + d + 1;

    if (d + 1 + len + 1 > in_len)
        return NS_ERR_TRUNC;
    if (payload[len] != ',')
        return NS_ERR_FORMAT;

    if (len > out_cap)
        return NS_ERR_LENGTH;

    /* A leading '!' asks for the normalized (upper-cased) form. */
    if (len > 0 && payload[0] == '!') {
        normalize_into_scratch(payload, len, out);
    } else {
        for (size_t i = 0; i < len; i++)
            out[i] = payload[i];
    }

    if (out_len)  *out_len  = len;
    if (consumed) *consumed = d + 1 + len + 1;
    return NS_OK;
}
