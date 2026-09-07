"""FuzzVerifier: score a battle on whether a fuzz harness builds + runs.

The full build+run path needs clang with libFuzzer; these cover the
logic that doesn't (toolchain missing, no harness, harness discovery)
and a real build only when clang is actually present.
"""
import shutil

import pytest

from gremlin_core.magic.fuzz_verifier import FuzzVerifier
from gremlin_core.magic.types import Task

_HAS_CLANG = bool(shutil.which("clang++") or shutil.which("clang"))
_TASK = Task(id="fz", prompt="write a fuzzer")


def test_missing_toolchain_is_scored_zero_with_a_clear_signal(tmp_path, monkeypatch):
    monkeypatch.setattr(FuzzVerifier, "_clang", lambda self: None)
    s = FuzzVerifier().score(_TASK, str(tmp_path))
    assert s.value == 0.0 and "clang" in s.failure_signal


def test_no_harness_found(tmp_path, monkeypatch):
    monkeypatch.setattr(FuzzVerifier, "_clang", lambda self: "clang")
    (tmp_path / "main.c").write_text("int main(){return 0;}\n")
    s = FuzzVerifier().score(_TASK, str(tmp_path))
    assert s.value == 0.0 and "harness" in s.failure_signal


def test_harness_discovery_picks_the_fuzz_file(tmp_path):
    (tmp_path / "parser.c").write_text("int parse(const char*p){return p?1:0;}\n")
    (tmp_path / "parser_fuzz.cc").write_text("x")
    v = FuzzVerifier()
    assert v._find_harness(tmp_path).name == "parser_fuzz.cc"


@pytest.mark.skipif(not _HAS_CLANG, reason="needs clang + libFuzzer")
def test_real_working_harness_scores_one(tmp_path):
    (tmp_path / "target_fuzz.cc").write_text(
        'extern "C" int LLVMFuzzerTestOneInput(const unsigned char *d, unsigned long s){\n'
        '  if (s >= 3 && d[0]==\'F\' && d[1]==\'U\' && d[2]==\'Z\') return 0;\n'
        '  return 0;\n}\n')
    s = FuzzVerifier(run_seconds=8).score(_TASK, str(tmp_path))
    assert s.value >= 1.0


@pytest.mark.skipif(not _HAS_CLANG, reason="needs clang + libFuzzer")
def test_a_harness_that_crashes_still_scores_one_and_reports_it(tmp_path):
    (tmp_path / "bug_fuzz.cc").write_text(
        'extern "C" int LLVMFuzzerTestOneInput(const unsigned char *d, unsigned long s){\n'
        '  char buf[4]; if (s) { for (unsigned long i=0;i<s;i++) buf[i]=d[i]; }\n'  # OOB write
        '  return 0;\n}\n')
    s = FuzzVerifier(run_seconds=20).score(_TASK, str(tmp_path))
    assert s.value >= 1.0 and "CRASH" in s.failure_signal.upper()


@pytest.mark.skipif(not _HAS_CLANG, reason="needs clang")
def test_uncompilable_harness_scores_zero(tmp_path):
    (tmp_path / "broken_fuzz.cc").write_text("this is not c++ at all;\n")
    s = FuzzVerifier().score(_TASK, str(tmp_path))
    assert s.value == 0.0 and "compile" in s.failure_signal.lower()
