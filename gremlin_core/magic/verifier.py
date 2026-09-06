"""Magic Verifier: run a project's pytest suite (MAGIC.md section 3).

    score          = passed / (passed + failed)   over the task's -k subset
    failure_signal = the failing test node ids

This is the whole of §7's "real anchor" for the first slice: a number
that comes from actually running the code, plus the names of what broke.
"""
from __future__ import annotations

import re
import subprocess
from pathlib import Path

from .types import Score, Task, Transcript

_SUMMARY_RE = re.compile(r"(\d+)\s+(passed|failed|error|errors|skipped|xfailed|xpassed)")
_FAILED_LINE_RE = re.compile(r"^(?:FAILED|ERROR)\s+(\S+)", re.MULTILINE)


class PytestVerifier:
    def __init__(self, python: str | None = None, test_timeout: int = 180):
        # Which interpreter runs pytest inside the target repo. Defaults to
        # the one running Einherjar (its venv has pytest).
        self.python = python or _self_python()
        self.test_timeout = test_timeout

    def score(self, task: Task, repo_path: str, transcript: Transcript | None = None) -> Score:
        cmd = [self.python, "-m", "pytest", "-q", "--no-header", "-rfE", "-p", "no:cacheprovider"]
        if task.test_filter:
            cmd += ["-k", task.test_filter]
        try:
            proc = subprocess.run(
                cmd, cwd=repo_path, capture_output=True, text=True, timeout=self.test_timeout,
            )
        except subprocess.TimeoutExpired:
            return Score(0.0, "pytest timed out", "timeout")

        out = (proc.stdout or "") + (proc.stderr or "")
        counts = {kind: int(n) for n, kind in _SUMMARY_RE.findall(out)}
        passed = counts.get("passed", 0)
        failed = counts.get("failed", 0) + counts.get("error", 0) + counts.get("errors", 0)

        total = passed + failed
        if total == 0:
            # no tests collected for this filter -> can't score it
            return Score(0.0, "no tests collected", out[-1500:])

        value = passed / total
        failing = _FAILED_LINE_RE.findall(out)
        signal = ""
        if failing:
            signal = f"{failed} failing: " + ", ".join(t.split("::")[-1] for t in failing)
        elif failed:
            signal = f"{failed} failing"
        return Score(value, signal, out[-2000:])


def _self_python() -> str:
    import sys
    return sys.executable
