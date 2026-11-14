"""Magic verifier for Android / Gradle projects.

PytestVerifier anchors a battle on `pytest`. This is the same idea for a
Gradle project: run a real gradle task -- `testDebugUnitTest` (compiles
everything AND runs the JVM unit tests) or `assembleDebug` -- and score
on whether it actually passes, with the compiler / test output fed back
as the failure signal. That turns "build an Android app" from the model
arguing with a reviewer into the model iterating against real `error:`
and test-failure output, exactly like PytestVerifier does for Python.

Uses the no-sudo toolchain env from android_build (JDK + SDK + Gradle),
so it needs `~/android-build/env.sh` + `~/Android/Sdk` present.
"""
from __future__ import annotations

import re
import subprocess
from pathlib import Path

from .types import Score, Task, Transcript
from .android_build import _toolchain_env, toolchain_ready

_ERR = re.compile(r"^.*?(error:|(?<!\w)e: |FAILED\b|What went wrong|Caused by:|> Task .*FAILED).*$",
                  re.MULTILINE)
_TEST_COUNT = re.compile(r"(\d+)\s+tests?\s+completed(?:,\s+(\d+)\s+failed)?")


class GradleVerifier:
    def __init__(self, task_label: str = "testDebugUnitTest",
                 build_timeout: int = 1200, offline: bool = True):
        self.task = task_label
        self.build_timeout = build_timeout
        self.offline = offline

    def score(self, task: Task, repo_path: str, transcript: Transcript | None = None) -> Score:
        root = Path(repo_path)
        if not toolchain_ready():
            return Score(0.0, "android toolchain not installed", "toolchain")

        gradlew = root / "gradlew"
        cmd = [("./gradlew" if gradlew.exists() else "gradle"),
               self.task, "--no-daemon", "--console=plain"]
        if self.offline:
            cmd.append("--offline")
        try:
            proc = subprocess.run(
                cmd, cwd=root, env=_toolchain_env(),
                capture_output=True, text=True, timeout=self.build_timeout,
            )
        except subprocess.TimeoutExpired:
            return Score(0.0, f"gradle {self.task} timed out after {self.build_timeout}s", "timeout")
        except OSError as e:
            return Score(0.0, f"could not run gradle: {e}", "toolchain")

        out = (proc.stdout or "") + (proc.stderr or "")

        if proc.returncode == 0 and "BUILD SUCCESSFUL" in out:
            return Score(1.0, "", out[-1500:])

        # partial credit from the test counts, if gradle got far enough to run them
        m = _TEST_COUNT.search(out)
        value = 0.0
        if m:
            total = int(m.group(1))
            failed = int(m.group(2) or 0)
            if total:
                value = max(0.0, (total - failed) / total) * 0.9  # cap < 1.0: build didn't pass

        seen, uniq = set(), []
        for line in _ERR.finditer(out):
            s = line.group(0).strip()
            if s and s not in seen:
                seen.add(s); uniq.append(s)
            if len(uniq) >= 15:
                break
        signal = ("gradle " + self.task + " failed:\n" + "\n".join(uniq)) if uniq \
            else f"gradle {self.task} failed (exit {proc.returncode})"
        return Score(value, signal, out[-3500:])
