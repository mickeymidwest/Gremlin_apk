"""
A structured record of every successful code change Magic has made
to itself (self_improve.py) or to a script it was asked to fix
(script_edit.py) -- separate from git history and backup files, this
is meant to be easy to scan or query later: what changed, why, and
when, in one place.
"""
from __future__ import annotations
import json
import os
import re
import time

# N15: redact anything secret-shaped before it lands in the audit log.
_SECRET_RES = [
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----", re.S),
    re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b"),          # AWS access key id
    re.compile(r"\bgh[pousr]_[A-Za-z0-9]{36,}\b"),          # GitHub token
    re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{10,}\b"),        # Slack token
    re.compile(r"\bsk-[A-Za-z0-9]{20,}\b"),                 # OpenAI-style key
    re.compile(r"\bAIza[0-9A-Za-z_\-]{35}\b"),             # Google API key
    re.compile(r"\b100\.(?:6[4-9]|[789]\d|1[01]\d|12[0-7])\.\d{1,3}\.\d{1,3}\b"),  # Tailscale CGNAT
]
# key=VALUE / key: VALUE with a long literal (12+ chars) -- not token=get_token()
_SECRET_KV = re.compile(
    r"(?i)\b(token|secret|password|passwd|api[_-]?key|access[_-]?key|auth)\b"
    r"(\s*[=:]\s*)['\"]?([A-Za-z0-9_+=/.\-]{12,})")


def _scrub_str(x: str) -> str:
    for rx in _SECRET_RES:
        x = rx.sub("[REDACTED]", x)
    return _SECRET_KV.sub(lambda m: m.group(1) + m.group(2) + "[REDACTED]", x)


def _scrub(obj):
    if isinstance(obj, str):
        return _scrub_str(obj)
    if isinstance(obj, dict):
        return {k: _scrub(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_scrub(v) for v in obj]
    return obj

LOG_FILENAME = "mutation_log.jsonl"


def _log_path(root: str) -> str:
    path = os.path.join(root, "data", LOG_FILENAME)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    return path


def append_mutation(root: str, entry: dict) -> None:
    entry = _scrub({"timestamp": time.time(), **entry})
    with open(_log_path(root), "a") as f:
        f.write(json.dumps(entry) + "\n")


def read_mutations(root: str, n: int = 50) -> list[dict]:
    """Last n entries, newest first -- roadmap #105/#106's /admin/log.
    Reads the whole file rather than seeking from the end: this log is a
    personal desktop's audit trail, not a firehose, so simplicity wins
    over a tail-from-the-end optimization that isn't needed yet."""
    path = _log_path(root)
    if not os.path.exists(path):
        return []
    out: list[dict] = []
    with open(path) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return list(reversed(out[-max(1, n):]))
