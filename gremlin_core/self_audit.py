"""N12 weekly self-audit: a read-only report that composes the checks Gremlin
already has -- desktop-guard posture, the N10 doc/config contradiction scan,
how fresh the last known-good checkpoint is, and disk/RAM headroom -- into one
plain summary. It CHANGES NOTHING; it just tells Mickey what's worth a look.

Run by hand or from a weekly timer:  python -m gremlin_core.self_audit [root]
"""
from __future__ import annotations

import re
import shutil
import subprocess
import sys
import time
from pathlib import Path


def _run(argv: list[str], timeout: float = 30.0) -> str:
    if not shutil.which(argv[0]):
        return ""
    try:
        r = subprocess.run(argv, capture_output=True, text=True, timeout=timeout,
                           stdin=subprocess.DEVNULL)
    except (subprocess.TimeoutExpired, OSError):
        return ""
    return r.stdout or ""


def known_good_age_days(root: str) -> "float | None":
    """Days since the newest known-good-* git tag, or None if there is none."""
    out = _run(["git", "-C", root, "for-each-ref", "--sort=-creatordate",
                "--format=%(creatordate:unix)", "refs/tags/known-good-*"])
    first = next((l.strip() for l in out.splitlines() if l.strip()), "")
    if not first.isdigit():
        return None
    return max(0.0, (time.time() - int(first)) / 86400.0)


def disk_free_gb(path: str = "/") -> "float | None":
    from . import resource
    return resource.disk_free_gb(path)


def mem_available_gb() -> "float | None":
    from . import resource
    return resource.ram_available_gb()


def compose(*, security: "list[tuple[str, str]]", drift: list, checkpoint_age_days,
            disk_free: "float | None", mem_avail: "float | None", bench: "list[str] | None" = None,
            stale_checkpoint_days: int = 3, low_disk_gb: float = 15.0,
            low_mem_gb: float = 0.4) -> str:
    """Pure formatter -> the report text. `security` is (level, what) pairs."""
    lines = [f"Gremlin weekly self-audit — {time.strftime('%Y-%m-%d %H:%M')}", ""]
    highs = [w for lvl, w in security if lvl == "high"]
    meds = [w for lvl, w in security if lvl == "medium"]
    lines.append(f"Security posture: {len(highs)} high, {len(meds)} medium (desktop guard)")
    for w in highs:
        lines.append(f"  [HIGH] {w}")
    for w in meds[:6]:
        lines.append(f"  [med]  {w}")

    lines.append("")
    if drift:
        lines.append(f"Doc/config drift: {len(drift)} contradiction(s) (run `gremlin contradictions`)")
    else:
        lines.append("Doc/config drift: none")

    lines.append("")
    if bench:
        lines.append(f"Bench: {len(bench)} regression(s) vs the best prior run")
        for b in bench[:6]:
            lines.append(f"  [REGRESSED] {b}")
    else:
        lines.append("Bench: no regression")

    lines.append("")
    if checkpoint_age_days is None:
        lines.append("Known-good checkpoint: NONE yet — run deploy/recovery/checkpoint.sh")
    elif checkpoint_age_days > stale_checkpoint_days:
        lines.append(f"Known-good checkpoint: STALE ({checkpoint_age_days:.1f} days old)")
    else:
        lines.append(f"Known-good checkpoint: fresh ({checkpoint_age_days:.1f} days old)")

    head = []
    if disk_free is not None:
        tag = " — LOW" if disk_free < low_disk_gb else ""
        head.append(f"disk {disk_free:.0f} GB free{tag}")
    if mem_avail is not None:
        tag = " — LOW" if mem_avail < low_mem_gb else ""
        head.append(f"RAM {mem_avail:.1f} GB available{tag}")
    if head:
        lines.append("Headroom: " + ", ".join(head))

    lines.append("")
    attention = bool(highs) or bool(drift) or bool(bench) or checkpoint_age_days is None or \
        (checkpoint_age_days or 0) > stale_checkpoint_days or \
        (disk_free is not None and disk_free < low_disk_gb) or \
        (mem_avail is not None and mem_avail < low_mem_gb)
    lines.append("=> Worth a look." if attention else "=> All clear.")
    return "\n".join(lines)


def run(root: str) -> str:
    from . import guard, contradiction
    try:
        findings = guard.standing(guard.snapshot())
        security = [(f.level, f.what) for f in findings]
    except Exception:
        security = []
    try:
        drift = contradiction.scan(root)
    except Exception:
        drift = []
    try:
        with open(str(Path(root) / "data" / "bench-regression.txt")) as f:
            bench = [l.strip() for l in f if l.strip()]
    except OSError:
        bench = []
    return compose(
        security=security, drift=drift, bench=bench,
        checkpoint_age_days=known_good_age_days(root),
        disk_free=disk_free_gb(), mem_avail=mem_available_gb())


def main() -> int:
    root = sys.argv[1] if len(sys.argv) > 1 else "."
    print(run(root))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
