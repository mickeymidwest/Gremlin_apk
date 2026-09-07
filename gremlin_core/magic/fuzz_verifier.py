"""Magic verifier for a fuzz harness.

PytestVerifier scores on `pytest`, GradleVerifier on a gradle build.
This scores a battle on whether a **libFuzzer / AFL++ harness actually
builds and runs** -- the anchor for "write a fuzzer for this parser".

score:
  1.0  -- harness builds with -fsanitize=fuzzer,address AND runs the
          time budget doing real executions (a working harness). If it
          finds a crash that's still 1.0 -- the harness did its job --
          and the crash is reported in the signal.
  0.5  -- builds and starts but does ~0 executions (dead harness: the
          entry point never actually exercises the target)
  0.0  -- won't compile, or the toolchain is missing

Needs clang with libFuzzer (`pacman -S clang`); see
deploy/setup-security-tools.sh.
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

from .types import Score, Task, Transcript

_HARNESS_RE = re.compile(r".*fuzz.*\.(c|cc|cpp|cxx)$", re.IGNORECASE)
_EXECS_RE = re.compile(r"#(\d+)\s")
_CRASH_RE = re.compile(r"(ERROR: (AddressSanitizer|libFuzzer):|SUMMARY: \w+Sanitizer|"
                       r"==\d+==ERROR|deadly signal|Test unit written to)")


class FuzzVerifier:
    def __init__(self, harness: str | None = None, run_seconds: int = 45,
                 build_timeout: int = 180):
        self.harness = harness            # explicit harness path, else auto-find "*fuzz*.c*"
        self.run_seconds = run_seconds
        self.build_timeout = build_timeout

    def _clang(self) -> str | None:
        return shutil.which("clang++") or shutil.which("clang")

    def _find_harness(self, root: Path) -> Path | None:
        if self.harness:
            p = root / self.harness
            return p if p.is_file() else None
        hits = [p for p in root.rglob("*")
                if p.is_file() and _HARNESS_RE.match(p.name)
                and ".git" not in p.parts]
        return hits[0] if hits else None

    def score(self, task: Task, repo_path: str, transcript: Transcript | None = None) -> Score:
        root = Path(repo_path)
        cc = self._clang()
        if cc is None:
            return Score(0.0, "clang not installed -- run deploy/setup-security-tools.sh "
                             "(pacman -S clang)", "toolchain")

        harness = self._find_harness(root)
        if harness is None:
            return Score(0.0, "no fuzz harness found (a *fuzz*.c/cc/cpp with "
                             "LLVMFuzzerTestOneInput). Write one.", "no-harness")

        # other C/C++ sources in the tree (the target under test), minus the harness
        srcs = [str(p) for p in root.rglob("*")
                if p.suffix.lower() in (".c", ".cc", ".cpp", ".cxx") and p != harness
                and ".git" not in p.parts and "test" not in p.name.lower()]

        work = Path(tempfile.mkdtemp(prefix="magic-fuzz-"))
        binp = work / "harness"
        cmd = [cc, "-g", "-O1", "-fno-omit-frame-pointer",
               "-fsanitize=fuzzer,address,undefined", str(harness), *srcs,
               "-I", str(root), "-o", str(binp)]
        try:
            b = subprocess.run(cmd, cwd=root, capture_output=True, text=True,
                               timeout=self.build_timeout)
        except (OSError, subprocess.TimeoutExpired) as e:
            shutil.rmtree(work, ignore_errors=True)
            return Score(0.0, f"build failed to run: {e}", "build")

        if b.returncode != 0 or not binp.exists():
            errs = [ln for ln in (b.stderr or "").splitlines()
                    if "error:" in ln or "undefined reference" in ln][:12]
            shutil.rmtree(work, ignore_errors=True)
            return Score(0.0, "harness won't compile:\n" + ("\n".join(errs) or (b.stderr or "")[-1500:]),
                         (b.stderr or "")[-2500:])

        corpus = work / "corpus"
        corpus.mkdir()
        (corpus / "seed").write_bytes(b"\x00\x01\x02\x03test")
        try:
            r = subprocess.run(
                [str(binp), str(corpus), f"-max_total_time={self.run_seconds}",
                 "-max_len=8192", "-print_final_stats=1", "-artifact_prefix=" + str(work) + "/"],
                cwd=work, capture_output=True, text=True, timeout=self.run_seconds + 60)
        except subprocess.TimeoutExpired:
            shutil.rmtree(work, ignore_errors=True)
            return Score(0.5, "harness ran but didn't exit on its own in the budget", "hang")

        out = (r.stdout or "") + (r.stderr or "")
        execs = [int(m.group(1)) for m in _EXECS_RE.finditer(out)]
        max_execs = max(execs) if execs else 0
        crash = bool(_CRASH_RE.search(out))
        shutil.rmtree(work, ignore_errors=True)

        if crash:
            tail = out[-1800:]
            return Score(1.0, f"working harness -- and it FOUND A CRASH in {max_execs} execs:\n{tail}", tail)
        if max_execs >= 500:
            return Score(1.0, "", f"harness built and ran clean: {max_execs} executions, no crash")
        return Score(0.5, f"harness builds and starts but only {max_execs} executions -- the entry "
                         "point probably isn't exercising the target. Check LLVMFuzzerTestOneInput "
                         "actually calls the parse function with data/size.", out[-1500:])
