# fuzz-practice — netstring parser

A tiny C netstring parser (`src/netstring.c` / `src/netstring.h`) with at
least one planted memory-safety bug. This is a **fuzzing practice target**
for Gremlin + Magic's `FuzzVerifier`.

## The job

Write a **libFuzzer harness** — a file matching `*fuzz*.c` / `*fuzz*.cc`
in this tree — that exercises `ns_parse` with the fuzzer's input so that a
build with `-fsanitize=fuzzer,address` finds the bug.

A good harness:

- defines `int LLVMFuzzerTestOneInput(const uint8_t *data, size_t size)`
- calls `ns_parse(data, size, out, out_cap, &out_len, &consumed)` with a
  real output buffer (pick an `out_cap`, e.g. 64, and a stack/heap buffer
  of that size — ASAN catches the overflow either way)
- returns 0
- does **not** call `exit()` or loop forever

Do **not** edit `src/netstring.c` to fix the bug — the harness has to make
the sanitizer catch it.

## The check

`FuzzVerifier` builds:

```
clang -g -O1 -fsanitize=fuzzer,address,undefined <harness> src/netstring.c -I . -o harness
```

then runs it for a time budget. Score:

- **1.0** — builds and does real executions (≥500), or finds a crash
- **0.5** — builds but the entry point never really exercises the target
- **0.0** — no harness, or it won't compile

Seeds for the corpus live in `corpus/`.
