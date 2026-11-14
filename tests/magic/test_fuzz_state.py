"""Fuzzing crash memory: signature extraction + dedup/clustering + persistence."""
from pathlib import Path

from magic_core.magic import fuzz_state as FS

_ASAN = """\
==12345==ERROR: AddressSanitizer: heap-buffer-overflow on address 0x60200000
READ of size 1 at 0x602000000011 thread T0
    #0 0x4a1b2c in parse_header /src/foo.c:42:10
    #1 0x4a2d3e in LLVMFuzzerTestOneInput /src/fuzz.c:10:3
    #2 0x51f in fuzzer::Fuzzer::ExecuteCallback(...) /llvm/FuzzerLoop.cpp
"""

_ASAN_SAME = _ASAN.replace("0x602000000011", "0x602000000099")   # same bug, diff addr
_SEGV = """\
==999==ERROR: AddressSanitizer: SEGV on unknown address 0x000000000000
    #0 0x4b0000 in write_cell /src/grid.c:88
    #1 0x4a2d3e in LLVMFuzzerTestOneInput /src/fuzz.c:10
"""


def test_signature_extracts_kind_and_site():
    assert FS.crash_signature(_ASAN) == "heap-buffer-overflow@parse_header"
    assert FS.crash_signature(_SEGV) == "segv@write_cell"


def test_signature_skips_runtime_frames():
    # the first non-runtime frame is the crash site, not fuzzer:: plumbing
    out = ("ERROR: AddressSanitizer: stack-overflow\n"
           "    #0 0x1 in fuzzer::Fuzzer::CrashCallback() /llvm/x.cpp\n"
           "    #1 0x2 in __sanitizer::Die() /s.cpp\n"
           "    #2 0x3 in recurse /src/a.c:5\n")
    assert FS.crash_signature(out) == "stack-overflow@recurse"


def test_record_dedups_same_signature(tmp_path):
    r = Path(tmp_path)
    assert FS.record_crash("tgt", output=_ASAN, artifact=b"AAAA", root=r) is True
    # same bug (same signature, different address/input) -> clustered, not new
    assert FS.record_crash("tgt", output=_ASAN_SAME, artifact=b"BBBB", root=r) is False
    # a genuinely different crash -> new
    assert FS.record_crash("tgt", output=_SEGV, artifact=b"CCCC", root=r) is True
    cs = FS.crashes("tgt", root=r)
    assert len(cs) == 2
    sigs = {c["sig"] for c in cs}
    assert sigs == {"heap-buffer-overflow@parse_header", "segv@write_cell"}


def test_artifact_bytes_persisted(tmp_path):
    r = Path(tmp_path)
    FS.record_crash("t2", output=_ASAN, artifact=b"\x00\x01crashme", root=r)
    c = FS.crashes("t2", root=r)[0]
    art = FS._dir("t2", r) / c["artifact"]
    assert art.read_bytes() == b"\x00\x01crashme"


def test_summary_and_targets_isolated(tmp_path):
    r = Path(tmp_path)
    FS.record_crash("alpha", output=_ASAN, artifact=b"x", root=r)
    assert "heap-buffer-overflow@parse_header" in FS.summary("alpha", root=r)
    # a different target has its own state
    assert FS.crashes("beta", root=r) == []
    assert "No distinct crashes" in FS.summary("beta", root=r)
