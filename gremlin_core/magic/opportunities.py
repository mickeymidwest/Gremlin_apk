"""Spot recurring asks in past use and turn them into skill candidates.

A self-improving agent shouldn't only learn from battles it was told to
run. This reads what mickey actually asks Gremlin -- the conversation
threads and the learning log -- clusters near-duplicate requests, and
surfaces the clusters big enough to be worth a skill. `/skill suggest`.
"""
from __future__ import annotations

import json
import re
from collections import Counter
from pathlib import Path

_STOP = set("the a an to of in on for and or is are was were be do does did how "
            "what why when where can could would should i you it my me we our this "
            "that with from as at by".split())


def _keywords(text: str) -> frozenset:
    words = [w for w in re.findall(r"[a-z][a-z0-9_-]{2,}", (text or "").lower())
             if w not in _STOP]
    return frozenset(words)


def _user_prompts(project_root: str, limit: int = 400) -> list[str]:
    root = Path(project_root)
    out: list[str] = []
    for p in sorted((root / "data" / "conversations").glob("*.jsonl")):
        for line in p.read_text(errors="ignore").splitlines()[-limit:]:
            try:
                out.append(json.loads(line).get("user", ""))
            except ValueError:
                pass
    log = root / "data" / "learning_log.jsonl"
    if log.exists():
        for line in log.read_text(errors="ignore").splitlines()[-limit:]:
            try:
                out.append(json.loads(line).get("prompt", ""))
            except ValueError:
                pass
    return [p for p in out if p and len(p) > 8]


def find(project_root: str, min_cluster: int = 3) -> list[dict]:
    """Return clusters of similar past asks: {size, sample, keywords}."""
    prompts = _user_prompts(project_root)
    if len(prompts) < min_cluster:
        return []
    sigs = [(_keywords(p), p) for p in prompts]
    used = [False] * len(sigs)
    clusters = []
    for i, (ki, pi) in enumerate(sigs):
        if used[i] or len(ki) < 2:
            continue
        members = [pi]
        used[i] = True
        for j in range(i + 1, len(sigs)):
            kj, pj = sigs[j]
            if used[j]:
                continue
            overlap = len(ki & kj)
            if overlap >= 2 and overlap >= 0.5 * min(len(ki), len(kj)):
                members.append(pj)
                used[j] = True
        if len(members) >= min_cluster:
            common = Counter()
            for m in members:
                common.update(_keywords(m))
            clusters.append({
                "size": len(members),
                "sample": members[0][:120],
                "keywords": [w for w, _ in common.most_common(6)],
            })
    return sorted(clusters, key=lambda c: -c["size"])
