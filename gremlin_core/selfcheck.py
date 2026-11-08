"""Self-check (R7): compare the two most recent benchmark runs of a model
and flag any part that got worse. Pure and no GPU -- it reads the bench
jsonl that bench/quality.py already writes. The nightly timer (opt-in) runs
a fresh bench first, then this; or run `gremlin selfcheck` any time.

A drop of more than TOLERANCE on any scored part (quality, heldout,
realtask, stop, honesty, honesty_ho, norefuse, norefuse_hard, chat) is a
regression worth a look -- e.g. after a self-edit or a config change.
"""
from __future__ import annotations

import glob
import json
import os

TOLERANCE = 1  # run-to-run noise is about +/-1 task; a bigger drop is real


def _runs(bench_dir: str) -> list[dict]:
    rows = []
    for fp in sorted(glob.glob(os.path.join(bench_dir, "quality-*.jsonl"))):
        try:
            for line in open(fp):
                r = json.loads(line)
                if r.get("suite") and r.get("parts"):
                    rows.append(r)
        except (OSError, ValueError):
            continue
    return rows


def _key(r: dict) -> str:
    return r.get("name") or r.get("model") or "?"


def compare(bench_dir: str, model: str | None = None) -> tuple[bool, str]:
    """(regressed?, report). Compares, per model and suite, the two most
    recent runs with the SAME parts, flagging parts that dropped by more
    than TOLERANCE. model=None checks every model that has two runs."""
    runs = _runs(bench_dir)
    if not runs:
        return False, "No benchmark runs recorded yet."
    # group by (name, suite); keep chronological (file + line order)
    groups: dict[tuple, list[dict]] = {}
    for r in runs:
        if model and _key(r) != model:
            continue
        groups.setdefault((_key(r), r["suite"]), []).append(r)

    regressed = False
    lines: list[str] = []
    for (name, _suite), rs in sorted(groups.items()):
        if len(rs) < 2:
            continue
        prev, cur = rs[-2], rs[-1]
        drops = []
        for part, p in cur["parts"].items():
            if part not in prev["parts"]:
                continue
            before = prev["parts"][part]["score"]
            after = p["score"]
            if after < before - TOLERANCE:
                drops.append(f"{part} {before}->{after}")
        if drops:
            regressed = True
            lines.append(f"REGRESSION  {name}: " + ", ".join(drops)
                         + f"  ({prev['ts'][:10]} -> {cur['ts'][:10]})")
        else:
            lines.append(f"ok  {name}: no drop since {prev['ts'][:10]}")
    if not lines:
        return False, "Not enough history yet (need two runs of a model on the same suite)."
    head = "SELF-CHECK: a regression was found.\n" if regressed else "SELF-CHECK: all good.\n"
    return regressed, head + "\n".join(lines)
