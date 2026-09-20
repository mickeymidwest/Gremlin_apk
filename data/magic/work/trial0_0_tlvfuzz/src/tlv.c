#include "tlv.h"

#include <stdlib.h>
#include <string.h>

static uint32_t rd_u32(const uint8_t *p) {
    return (uint32_t)p[0] | ((uint32_t)p[1] << 8) |
           ((uint32_t)p[2] << 16) | ((uint32_t)p[3] << 24);
}
static uint16_t rd_u16(const uint8_t *p) {
    return (uint16_t)((uint16_t)p[0] | ((uint16_t)p[1] << 8));
}

/* header: magic(4) + count(2) */
#define TLV_HDR 6
/* per-record fixed part: type(1) + len(2) */
#define REC_HDR 3
/* refuse a single record longer than this */
#define REC_MAX 4096

tlv_status tlv_parse(const uint8_t *in, size_t in_len, TlvDoc *doc) {
    memset(doc, 0, sizeof(*doc));
    if (in_len < TLV_HDR)
        return TLV_ERR_TRUNC;
    if (rd_u32(in) != TLV_MAGIC)
        return TLV_ERR_MAGIC;

    uint16_t count = rd_u16(in + 4);
    if (count == 0)
        return TLV_OK;                     /* empty doc, nothing allocated */

    /* One pass to size the value blob, and to sanity-check framing. */
    size_t blob_len = 0;
    size_t pos = TLV_HDR;
    for (uint16_t i = 0; i < count; i++) {
        if (pos + REC_HDR > in_len)
            return TLV_ERR_TRUNC;
        uint16_t len = rd_u16(in + pos + 1);
        if (len > REC_MAX)
            return TLV_ERR_TOOBIG;
        if (pos + REC_HDR + len > in_len)
            return TLV_ERR_TRUNC;
        blob_len += len;
        pos += REC_HDR + len;
    }

    doc->records = (TlvRecord *)calloc(count, sizeof(TlvRecord));
    doc->blob    = (uint8_t *)malloc(blob_len ? blob_len : 1);
    if (!doc->records || !doc->blob) {
        free(doc->records); free(doc->blob);
        memset(doc, 0, sizeof(*doc));
        return TLV_ERR_NOMEM;
    }

    /* Second pass: actually copy. The loop condition here is "while there
     * are still bytes for another record", NOT "i < count" -- so a stream
     * with more records than the header claimed keeps writing past the
     * end of doc->records. */
    size_t blob_off = 0;
    uint16_t n = 0;
    pos = TLV_HDR;
    while (pos + REC_HDR <= in_len) {
        uint8_t  type = in[pos];
        uint16_t len  = rd_u16(in + pos + 1);
        if (len > REC_MAX || pos + REC_HDR + len > in_len)
            break;
        doc->records[n].type   = type;      /* BUG: n can exceed `count` */
        doc->records[n].len    = len;
        doc->records[n].offset = blob_off;
        memcpy(doc->blob + blob_off, in + pos + REC_HDR, len);
        blob_off += len;
        pos      += REC_HDR + len;
        n++;
    }

    doc->blob_len = blob_off;
    doc->count    = n;
    return TLV_OK;
}

void tlv_free(TlvDoc *doc) {
    if (!doc) return;
    free(doc->records);
    free(doc->blob);
    memset(doc, 0, sizeof(*doc));
}

uint32_t tlv_total_len_of_type(const TlvDoc *doc, uint8_t type) {
    uint32_t total = 0;
    for (uint16_t i = 0; i < doc->count; i++)
        if (doc->records[i].type == type)
            total += doc->records[i].len;
    return total;
}
