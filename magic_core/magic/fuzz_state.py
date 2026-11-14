"""Fuzzing crash memory (Magic's request, turn 95): the FuzzVerifier used to
find a crash and then delete the crashing input along with its temp dir. This
remembers each UNIQUE crash per target -- signature, a snippet, and the input
bytes that triggered it -- deduped/clustered by signature across runs, so a
fuzzing session builds a real set of distinct bugs instead of re-finding the
same one every night.

Deterministic, no model. Stored under <repo>/data/fuzz_state/<target>/.
"""
from __future__ import annotations

import hashlib
import json
import re
import time
from pathlib import Path

_REPO = Path(__file__).resolve().parents[2]

# the sanitizer / libFuzzer error class
_TYPE = re.compile(
    r"(?:AddressSanitizer|UndefinedBehaviorSanitizer|LeakSanitizer|libFuzzer):\s*"
    r"([A-Za-z][A-Za-z0-9 _-]*?)(?: on | in |\n|,|:)")
# a backtrace frame:  #3 0xabc in func /path:line
_FRAME = re.compile(r"#\d+\s+0x[0-9a-f]+\s+in\s+(\S+)")
# frames that are runtime plumbing, not the bug site
_RUNTIME = ("fuzzer::", "__sanitizer", "asan_", "__asan", "__interceptor",
            "ExecuteCallback", "RunOneTest", "FuzzerDriver")


def crash_signature(output: str) -> str:
    """A stable id for a crash: '<error-kind>@<crash-site-func>'. Two crashes
    with the same signature are the same bug (clustered together)."""
    t = _TYPE.search(output or "")
    kind = (t.group(1).strip() if t else "crash").replace(" ", "-").lower()
    func = ""
    for m in _FRAME.finditer(output or ""):
        fn = m.group(1)
        if any(r in fn for r in _RUNTIME):
            continue
        func = fn
        break
    return f"{kind}@{func}" if func else kind


def _dir(target: str, root: Path | None = None) -> Path:
    safe = re.sub(r"[^A-Za-z0-9_.-]", "_", target or "target")[:80]
    return (root or _REPO) / "data" / "fuzz_state" / safe


def record_crash(target: str, *, output: str, artifact: bytes | None = None,
                 root: Path | None = None) -> bool:
    """Record a crash if its signature is new for this target. Returns True if
    it was NEW (a distinct bug), False if it clustered into a known one."""
    sig = crash_signature(output)
    d = _dir(target, root)
    log = d / "crashes.jsonl"
    try:
        known = {json.loads(l)["sig"] for l in log.read_text().splitlines() if l.strip()}
    except OSError:
        known = set()
    if sig in known:
        return False
    try:
        d.mkdir(parents=True, exist_ok=True)
        entry = {"sig": sig, "at": time.time(), "snippet": (output or "")[-600:]}
        if artifact is not None:
            h = hashlib.sha1(artifact).hexdigest()[:16]
            art_path = d / f"crash-{h}.bin"
            art_path.write_bytes(artifact)
            entry["artifact"] = art_path.name
        with open(log, "a") as f:
            f.write(json.dumps(entry) + "\n")
        return True
    except OSError:
        return False


def crashes(target: str, root: Path | None = None) -> list[dict]:
    """The distinct crashes recorded for a target, newest first."""
    try:
        rows = [json.loads(l) for l in (_dir(target, root) / "crashes.jsonl").read_text().splitlines()
                if l.strip()]
    except (OSError, json.JSONDecodeError):
        return []
    return list(reversed(rows))


def summary(target: str, root: Path | None = None) -> str:
    cs = crashes(target, root)
    if not cs:
        return f"No distinct crashes recorded for {target!r}."
    lines = [f"{len(cs)} distinct crash(es) for {target!r}:"]
    for c in cs:
        art = f"  input: {c['artifact']}" if c.get("artifact") else ""
        lines.append(f"  {c['sig']}{art}")
    return "\n".join(lines)
