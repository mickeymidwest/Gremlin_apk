"""Where fine-tune data comes from.

The original pipeline trained only on learning_log.jsonl (the times a
fallback model had to rescue a failed local answer). That's clean signal
but slow to accumulate. This gathers training examples from every place
Gremlin + Magic already produce good (prompt -> answer) pairs, each
tagged with its source so a run can weight or cap them:

  learning_log   -- fallback rescued a failed local answer   (strongest signal)
  battle_win     -- a Magic battle that actually passed its verifier
  skill          -- a proven skill card, as "when X, do Y"
  conversation   -- real chat turns the user kept (not cleared, not errors)
  seed           -- data/finetune_seed/*.jsonl the user curated by hand

`gather(root)` returns a list of {"messages": [...], "source": "..."}.
build_training_dataset() folds these in alongside the learning log.
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Iterable


def _ex(user: str, assistant: str, source: str) -> dict | None:
    user, assistant = (user or "").strip(), (assistant or "").strip()
    if len(user) < 4 or len(assistant) < 8:
        return None
    return {"messages": [{"role": "user", "content": user},
                         {"role": "assistant", "content": assistant}],
            "source": source}


# -- battle wins -----------------------------------------------------

def _battle_wins(root: str) -> Iterable[dict]:
    ep_dir = Path(root) / "data" / "magic" / "episodes"
    if not ep_dir.is_dir():
        return
    for p in sorted(ep_dir.glob("*.json")):
        try:
            d = json.loads(p.read_text())
        except (OSError, ValueError):
            continue
        score = (d.get("score") or {}).get("value", 0)
        tr = d.get("transcript") or {}
        if score < 0.999:
            continue
        prompt = (d.get("task") or {}).get("prompt") or tr.get("task_id", "")
        # what the agent said it did + the files it touched
        final = tr.get("final_message", "")
        edits = [s for s in (tr.get("steps") or [])
                 if s.get("tool_name") in ("write_file", "edit_file") and s.get("content") == "ok"]
        touched = sorted({(s.get("tool_args") or {}).get("path", "") for s in edits} - {""})
        if not final and not touched:
            continue
        ans = final + (f"\n\n(changed: {', '.join(touched)})" if touched else "")
        e = _ex(prompt, ans, "battle_win")
        if e:
            yield e


# -- skill cards ---------------------------------------------------

def _skills(root: str) -> Iterable[dict]:
    sk_dir = Path(root) / "data" / "skills"
    if not sk_dir.is_dir():
        return
    import yaml
    for p in sorted(sk_dir.rglob("*.yaml")):
        try:
            c = yaml.safe_load(p.read_text()) or {}
        except (OSError, yaml.YAMLError):
            continue
        if c.get("status") == "deprecated" or not c.get("procedure"):
            continue
        trig = c.get("trigger_when") or c.get("purpose") or ""
        steps = "\n".join(f"{i}. {s}" for i, s in enumerate(c["procedure"], 1))
        user = f"What's the right approach when: {trig}?"
        assistant = f"{c.get('purpose','')}\n\n{steps}".strip()
        e = _ex(user, assistant, "skill")
        if e:
            yield e


# -- kept conversations ------------------------------------------

_SKIP_USER = re.compile(r"^(remember (that )?|forget|clear|reset|/|say |reply with|pong|ping)\b", re.I)


def _conversations(root: str, max_pairs: int = 400) -> Iterable[dict]:
    conv_dir = Path(root) / "data" / "conversations"
    if not conv_dir.is_dir():
        return
    n = 0
    for p in sorted(conv_dir.glob("*.jsonl")):
        try:
            lines = p.read_text(errors="ignore").splitlines()
        except OSError:
            continue
        for line in lines:
            try:
                rec = json.loads(line)
            except ValueError:
                continue
            if not isinstance(rec, dict):
                continue
            u, a = rec.get("user", ""), rec.get("assistant", "")
            if _SKIP_USER.match((u or "").strip()):
                continue
            if any(bad in (a or "").lower() for bad in (
                    "took too long", "hit an error", "couldn't get an answer", "try again")):
                continue
            e = _ex(u, a, "conversation")
            if e:
                yield e
                n += 1
                if n >= max_pairs:
                    return


# -- hand-curated seed -------------------------------------------

def _seed(root: str) -> Iterable[dict]:
    seed_dir = Path(root) / "data" / "finetune_seed"
    if not seed_dir.is_dir():
        return
    for p in sorted(seed_dir.glob("*.jsonl")):
        for line in p.read_text(errors="ignore").splitlines():
            try:
                rec = json.loads(line)
            except ValueError:
                continue
            if isinstance(rec, dict) and rec.get("messages"):
                rec.setdefault("source", "seed")
                yield rec
            elif isinstance(rec, dict):
                e = _ex(rec.get("prompt", rec.get("user", "")),
                        rec.get("answer", rec.get("assistant", "")), "seed")
                if e:
                    yield e


# -- everything --------------------------------------------------

def gather(root: str) -> list[dict]:
    out: list[dict] = []
    for fn in (_battle_wins, _skills, _conversations, _seed):
        try:
            out.extend(fn(root))
        except Exception:  # noqa -- one bad source must not sink the run
            pass
    # dedupe on the user message
    seen: set[str] = set()
    uniq = []
    for e in out:
        key = e["messages"][0]["content"].strip().lower()[:200]
        if key not in seen:
            seen.add(key)
            uniq.append(e)
    return uniq


def counts(root: str) -> dict[str, int]:
    c: dict[str, int] = {}
    for e in gather(root):
        c[e["source"]] = c.get(e["source"], 0) + 1
    return c
