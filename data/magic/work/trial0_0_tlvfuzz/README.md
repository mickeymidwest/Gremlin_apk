# fuzz-practice-2 — binary TLV container parser

A harder fuzzing target than `fuzz-practice`. `src/tlv.c` parses a
little-endian tag-length-value binary format:

```
magic  u32 = 0x564C5401 ("TLV\1")
count  u16          number of records
records  count *  { type u8 ; len u16 ; value[len] }
```

`tlv_parse()` allocates a record array and a flat value blob, fills a
`TlvDoc`, and the caller frees it with `tlv_free()`. There is (at least
one) planted memory-safety bug on the parse path. It is **not** a simple
off-by-one — you have to feed structurally valid-ish binary to reach it.

## The job

Write a **libFuzzer harness** (`*fuzz*.c` / `*fuzz*.cc` in this tree) that:

- `#include "src/tlv.h"`
- defines `int LLVMFuzzerTestOneInput(const uint8_t *data, size_t size)`
- declares a `TlvDoc doc;` (that is the exact struct name — check tlv.h),
  calls `tlv_parse(data, size, &doc)`, and on `TLV_OK` also calls
  `tlv_free(&doc)` (so the fuzzer doesn't leak every run) — calling
  `tlv_total_len_of_type(&doc, data[0])` too exercises more surface
- the build links `src/tlv.c` alongside your harness:
  `clang -fsanitize=fuzzer,address,undefined <harness>.c src/tlv.c -I . -o harness`
- returns 0, never calls `exit()`, never loops forever

Seed inputs are in `corpus/` — real valid documents. libFuzzer mutates
from those, so the magic stays intact and it can get deep quickly.

Do **not** edit `src/tlv.c`.

## The check

`FuzzVerifier` builds with `clang -fsanitize=fuzzer,address,undefined`
and runs the harness for a time budget. **1.0** = builds + real
executions (≥500) or a crash; **0.5** = builds but the entry point never
really drives the parser; **0.0** = no harness / won't compile.
