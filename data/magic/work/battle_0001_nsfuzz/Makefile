harness_fuzz: harness_fuzz.c src/netstring.h
    clang++ -fsanitize=fuzzer,address -I . -o $@ $<