"""R7 bench-regression check: compare the newest quality-bench result for a
model against its best PRIOR run with the same suite hash, and flag any part
whose score dropped by >= min_drop. No model calls -- it only reads the
bench/quality-*.jsonl files quality.py already writes, so it's cheap and can
run right after the nightly bench (or be surfaced by `magic self-audit`).

Writes data/bench-regression.txt when something regressed (removes it when
clean), so other tools can surface it without importing this.
"""
from __future__ import annotations

import glob
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ALERT = os.path.join(os.path.dirname(HERE), "data", "bench-regression.txt")


def load_runs(paths: "list[str] | None" = None) -> list[dict]:
    runs: list[dict] = []
    for fp in sorted(paths or glob.glob(os.path.join(HERE, "quality-*.jsonl"))):
        try:
            with open(fp) as f:
                for line in f:
                    line = line.strip()
                    if line:
                        runs.append(json.loads(line))
        except (OSError, json.JSONDecodeError):
            continue
    return runs


def regressions(runs: list[dict], *, min_drop: int = 2) -> list[str]:
    """One line per part that dropped vs the best earlier run of the same
    (model, suite). Needs at least one prior run to compare against -- a first
    run can't be a regression."""
    out: list[str] = []
    by_key: "dict[tuple, list[dict]]" = {}
    for r in runs:                       # file + append order is chronological
        by_key.setdefault((r.get("model"), r.get("suite")), []).append(r)
    for (model, _suite), rs in by_key.items():
        if len(rs) < 2:
            continue
        newest, prior = rs[-1], rs[:-1]
        for part, pv in (newest.get("parts") or {}).items():
            best_prior = max(
                (p["parts"][part]["score"] for p in prior
                 if isinstance(p.get("parts"), dict) and part in p["parts"]),
                default=None)
            if best_prior is None:
                continue
            now = pv.get("score", 0)
            if best_prior - now >= min_drop:
                out.append(
                    f"{model}: {part} dropped to {now}/{pv.get('total')} "
                    f"(best was {best_prior}) -- possible regression")
    return out


def write_alert(items: list[str]) -> None:
    try:
        if items:
            os.makedirs(os.path.dirname(ALERT), exist_ok=True)
            with open(ALERT, "w") as f:
                f.write("\n".join(items) + "\n")
        elif os.path.exists(ALERT):
            os.remove(ALERT)
    except OSError:
        pass


def current_alert() -> list[str]:
    """The last recorded regression lines (for self-audit), or []."""
    try:
        with open(ALERT) as f:
            return [l.strip() for l in f if l.strip()]
    except OSError:
        return []


def main() -> int:
    items = regressions(load_runs(), min_drop=2)
    write_alert(items)
    if items:
        print("Bench regression detected:")
        for i in items:
            print("  " + i)
        return 1
    print("No bench regression vs the best prior run.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
