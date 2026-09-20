/* A toy tag-length-value container format, the kind a binary protocol or
 * a file format uses.  Layout (all integers little-endian):
 *
 *   magic   u32   = 0x564C5401  ("TLV\1")
 *   count   u16         number of records that follow
 *   records count *   { type u8 ; len u16 ; value[len] }
 *
 * tlv_parse() validates the header and copies every record's value into a
 * single flat buffer it allocates, returning that buffer + the per-record
 * offsets so a caller can walk them.
 *
 * This is a fuzzing-practice target.  There is (at least) one planted
 * memory-safety bug on the parse path.  Do not "fix" the .c file -- write
 * a harness that makes a sanitizer catch it.
 */
#ifndef TLV_H
#define TLV_H

#include <stddef.h>
#include <stdint.h>

#ifdef __cplusplus
extern "C" {
#endif

#define TLV_MAGIC 0x564C5401u

typedef enum {
    TLV_OK = 0,
    TLV_ERR_MAGIC   = -1,
    TLV_ERR_TRUNC   = -2,   /* input ends mid-record            */
    TLV_ERR_TOOBIG  = -3,   /* a record / the total is too large */
    TLV_ERR_NOMEM   = -4
} tlv_status;

typedef struct {
    uint8_t  type;
    uint16_t len;
    size_t   offset;        /* into TlvDoc.blob */
} TlvRecord;

typedef struct {
    uint8_t   *blob;        /* all record values, concatenated */
    size_t     blob_len;
    TlvRecord *records;
    uint16_t   count;
} TlvDoc;

/* Parse `in`/`in_len`.  On TLV_OK, *doc is filled and owns two heap
 * allocations (doc->blob, doc->records); call tlv_free(doc) to release
 * them.  On any error nothing is allocated. */
tlv_status tlv_parse(const uint8_t *in, size_t in_len, TlvDoc *doc);

void tlv_free(TlvDoc *doc);

/* Convenience: sum of the lengths of all records of a given type. */
uint32_t tlv_total_len_of_type(const TlvDoc *doc, uint8_t type);

#ifdef __cplusplus
}
#endif

#endif
