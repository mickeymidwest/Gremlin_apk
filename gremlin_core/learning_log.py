"""data/learning_log.jsonl -- what `gremlin finetune` trains on.

One line per "Gremlin didn't know this on its own" moment: the prompt,
the answer that was eventually good, and (historically) which models
contributed. Written from the chat path, read by finetune.py /
distill.py / checkpoint_eval.py.

Split out of the old consult.py so the learning log outlives the
specialist-consult machinery it used to live next to.
"""
from __future__ import annotations

import json
import os
import time
from typing import Optional


def log_path(root: str) -> str:
    path = os.path.join(root, "data", "learning_log.jsonl")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    return path


def load_learned_answer(root: str, prompt: str) -> Optional[str]:
    """Exact-match lookup against past entries. Deliberately simple -- no
    embeddings, no fuzzy match; a resumability shortcut, not a memory."""
    path = log_path(root)
    if not os.path.exists(path):
        return None
    with open(path, "r") as f:
        for line in f:
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                continue
            if entry.get("prompt") == prompt:
                return entry.get("final_answer")
    return None


def append_learning_log(root: str, entry: dict) -> None:
    entry.setdefault("timestamp", time.time())
    with open(log_path(root), "a") as f:
        f.write(json.dumps(entry) + "\n")
