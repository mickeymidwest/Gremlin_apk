# fuzz-practice-3 — INI config parser

`src/ini.c` parses INI-style text (`[section]`, `key = value`, `#` comments)
and `ini_get(text, len, "section.key", out, out_cap)` returns the value.
There is (at least one) planted stack-buffer-overflow on the parse path.

## The job

Write a **libFuzzer harness** (`*fuzz*.c` in this tree) that:

- `#include "src/ini.h"`
- defines `int LLVMFuzzerTestOneInput(const uint8_t *data, size_t size)`
- calls `ini_get((const char*)data, size, "a.b", out, sizeof out)` with a
  real `char out[64];` -- and calling `ini_count((const char*)data, size)`
  too covers the other entry point
- returns 0, never `exit()`, never loops forever

Build (the check does this):
`clang -g -O1 -fsanitize=fuzzer,address,undefined <harness>.c src/ini.c -I . -o harness`

Don't patch `src/ini.c`. Score: 1.0 = builds + real executions or a crash;
0.5 = builds but the entry point never drives the parser; 0.0 = no harness
or won't compile. Seeds are in `corpus/`.
