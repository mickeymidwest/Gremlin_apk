"""
Natural-language intent routing -- talk to Gremlin the way you'd talk to
a person, not to a command line.

Before this, doing anything real on the desktop meant typing a slash
command with exact arguments: `/updatecheck`, `/fix /home/mickey/x.sh
it's broken confirm`, `/rollback 42 confirm`. That's a CLI wearing a
chat costume. The point of this module is that mid-conversation about
anything at all, "hey can you check for OS updates" or "something's
wrong with my backup script, fix it" just works.

Three deliberate design choices:

1. **Cost nothing on normal chat.** Classifying every message with a
   model call would make ordinary conversation slower for the sake of
   the rare action message. So there's a cheap regex pre-filter first:
   only if a message contains an action-shaped signal (a system noun
   like "update"/"snapshot"/"reboot", or an imperative aimed at
   Gremlin itself) does it cost one classification call. Talking about
   birds never touches the classifier.

2. **Never guess destructively.** Read-only actions (update check,
   listing snapshots) just run. Anything that changes the machine
   (reboot, rollback, editing files, running commands) comes back as a
   *pending confirmation* first -- but phrased as a normal question
   ("that'll reboot the desktop now, want me to?"), answered by a
   normal "yeah do it", not a `confirm` suffix. Same safety as the old
   two-step slash commands, none of the syntax.

3. **Paths are found, not typed.** "fix my backup script" shouldn't
   require knowing it lives at ~/bin/backup.sh. find_file() scans the
   project and the usual home directories and fuzzy-matches. If it's
   ambiguous it asks which one instead of picking blindly.

The `/claude` override is intentionally NOT routed through here -- it
stays an explicit, typed escape hatch, because handing full autonomy
to a separate Claude Code session is exactly the thing that should
require deliberate intent rather than a classifier's best guess.
"""
from __future__ import annotations

import json
import os
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

from .router import Router

# ---------------------------------------------------------------- actions

# Read-only actions run immediately. Mutating ones always confirm first.
READ_ONLY_ACTIONS = {"update_check", "snapshots", "run_command_read", "find_file"}
MUTATING_ACTIONS = {"self_edit", "script_fix", "run_command", "rollback", "reboot"}
ALL_ACTIONS = READ_ONLY_ACTIONS | MUTATING_ACTIONS | {"chat"}

# Words that make a message *possibly* an action request. This is a
# recall-oriented filter, not precision -- a false positive here just
# costs one classification call, a false negative means the feature
# silently doesn't work, so it errs toward matching.
_TRIGGER_PATTERN = re.compile(
    r"""\b(
        update|updates|upgrade|patch(es)?|pacman|package
      | reboot|restart|shut\s*down
      | snapshot|snapshots|roll\s*back|rollback|revert|restore
      | fix|broken|bug|error|failing|crash(ing|ed)?|repair
      | install|uninstall|remove
      | run|execute|launch|start|stop|kill
      | disk|memory|ram|cpu|df|uptime|process(es)?|service|systemctl
      | (add|give|build|make|write|teach|learn|wire)\b[^.?!]{0,60}?\bto\s+(you|yourself|your\s+own)\b
      | teach\s+yourself|improve\s+yourself|edit\s+yourself|change\s+yourself|update\s+yourself
      | you\s+should\s+(be\s+able\s+to|have|support|know\s+how)
      | can\s+you\s+(learn|add|support|handle)
      | your\s+(own\s+)?(code|source|self)
      | script|\.sh\b|\.py\b|\.conf\b|\.yaml\b|\.yml\b
      | check\s+(for|the|my|if)
    )\b""",
    re.IGNORECASE | re.VERBOSE,
)

# Directly-answerable without any model call at all -- unambiguous
# enough that spending a classification call on them is pure waste.
_FAST_PATHS: list[tuple[re.Pattern, str]] = [
    (re.compile(r"\b(check|any|are there|is there|look for)\b.{0,25}\b(os|system|pacman|package|software)?\s*updates?\b", re.I), "update_check"),
    (re.compile(r"\bupdate\s*check\b", re.I), "update_check"),
    (re.compile(r"\b(list|show|what)\b.{0,20}\bsnapshots?\b", re.I), "snapshots"),
    (re.compile(r"\breboot\b.{0,20}\b(desktop|machine|computer|pc|it)\b", re.I), "reboot"),
    (re.compile(r"^\s*reboot\s*$", re.I), "reboot"),
]

_CLASSIFY_PROMPT = """You are an intent classifier for a personal assistant that controls a Linux desktop.
Classify the user's message into EXACTLY ONE action and extract its arguments.

Actions:
- chat: ordinary conversation, questions, explanations. THE DEFAULT. Use this unless the user is clearly asking for something to be DONE to the machine or to your own code.
- update_check: check whether OS/package updates are pending and whether they're safe.
- snapshots: list filesystem snapshots.
- rollback: roll back to a snapshot. args: {"number": "<snapshot number>"}
- reboot: reboot the desktop.
- self_edit: change Gremlin's OWN code/behavior/capabilities ("add X to yourself", "you should be able to Y"). args: {"goal": "<what to change>"}
- script_fix: fix a file that is NOT Gremlin's own code (a user script, config, etc). args: {"file_hint": "<name or description of the file>", "problem": "<what's wrong>"}
- run_command: run a shell command on the desktop. args: {"command": "<the command>"}

Respond with ONLY a JSON object, no prose, no markdown fence:
{"action": "<action>", "args": {...}, "confidence": <0.0-1.0>}

If you are not confident the user wants a real action performed, answer {"action": "chat", "args": {}, "confidence": 1.0}.

User message: """


@dataclass
class Intent:
    action: str
    args: dict[str, Any] = field(default_factory=dict)
    confidence: float = 0.0
    # Populated when the action needs the user to say yes before it runs.
    needs_confirmation: bool = False
    confirmation_prompt: str = ""

    @property
    def is_chat(self) -> bool:
        return self.action == "chat"


def looks_like_action(message: str) -> bool:
    """Cheap pre-filter: is it even worth asking the model to classify?

    Deliberately generous -- a false positive costs one fast local call,
    a false negative means "hey reboot the desktop" silently gets
    answered as conversation."""
    return bool(_TRIGGER_PATTERN.search(message or ""))


def fast_path(message: str) -> Optional[str]:
    """Unambiguous phrasings that don't need a model call at all."""
    text = (message or "").strip()
    for pattern, action in _FAST_PATHS:
        if pattern.search(text):
            return action
    return None


def _parse_classification(raw: str) -> Optional[Intent]:
    """Models love wrapping JSON in prose or fences -- dig the object out
    rather than demanding perfectly clean output."""
    if not raw:
        return None
    text = raw.strip()
    # Strip a ```json fence if there is one.
    fence = re.search(r"```(?:json)?\s*(.+?)```", text, re.DOTALL)
    if fence:
        text = fence.group(1).strip()
    # Otherwise find the outermost {...}.
    if not text.startswith("{"):
        start = text.find("{")
        end = text.rfind("}")
        if start == -1 or end == -1 or end <= start:
            return None
        text = text[start:end + 1]
    try:
        data = json.loads(text)
    except (ValueError, TypeError):
        return None
    if not isinstance(data, dict):
        return None

    action = str(data.get("action", "chat")).strip().lower()
    if action not in ALL_ACTIONS:
        return None
    args = data.get("args") or {}
    if not isinstance(args, dict):
        args = {}
    try:
        confidence = float(data.get("confidence", 0.0))
    except (TypeError, ValueError):
        confidence = 0.0
    return Intent(action=action, args=args, confidence=confidence)


async def classify(router: Router, model_name: str, message: str, min_confidence: float = 0.6) -> Intent:
    """Decide what the user actually wants.

    Order matters: fast path (free) -> pre-filter (free) -> model call
    (only for messages that plausibly want something done). Anything the
    model isn't confident about falls back to plain chat, because the
    cost of misrouting a conversation into a system action is much
    higher than the cost of answering an action request conversationally
    (the user can just rephrase)."""
    text = (message or "").strip()
    if not text:
        return Intent(action="chat", confidence=1.0)

    quick = fast_path(text)
    if quick:
        return _finalize(Intent(action=quick, args={}, confidence=1.0), text)

    if not looks_like_action(text):
        return Intent(action="chat", confidence=1.0)

    try:
        result = await router.route(model_name, _CLASSIFY_PROMPT + text)
    except Exception:
        return Intent(action="chat", confidence=1.0)
    if not result.ok:
        return Intent(action="chat", confidence=1.0)

    parsed = _parse_classification(result.text)
    if parsed is None or parsed.is_chat or parsed.confidence < min_confidence:
        return Intent(action="chat", confidence=1.0)

    return _finalize(parsed, text)


def _finalize(intent: Intent, original_message: str) -> Intent:
    """Attach confirmation requirements + human-readable prompts."""
    if intent.action in MUTATING_ACTIONS:
        intent.needs_confirmation = True
        intent.confirmation_prompt = _confirmation_text(intent, original_message)
    return intent


def _confirmation_text(intent: Intent, original_message: str) -> str:
    a = intent.args
    if intent.action == "reboot":
        return "That'll reboot the desktop right now. Want me to go ahead?"
    if intent.action == "rollback":
        number = a.get("number", "?")
        return (
            f"That rolls the system back to snapshot {number} and reboots to do it. "
            "Anything unsaved goes away. Want me to go ahead?"
        )
    if intent.action == "self_edit":
        goal = a.get("goal") or original_message
        return (
            f"I'd be rewriting my own code for that: \"{goal}\". Two other models review the "
            "patch and it only lands if both approve, and it's committed to git either way so "
            "it's revertible. Want me to go ahead?"
        )
    if intent.action == "script_fix":
        path = a.get("resolved_path") or a.get("file_hint", "that file")
        problem = a.get("problem", "")
        tail = f" ({problem})" if problem else ""
        return (
            f"I'd edit {path}{tail}. I back it up first and revert automatically if the fix "
            "doesn't compile. Want me to go ahead?"
        )
    if intent.action == "run_command":
        return f"I'd run this on the desktop:\n\n    {a.get('command', '')}\n\nWant me to go ahead?"
    return "Want me to go ahead?"


# ------------------------------------------------------- confirmation state

# A yes/no answer only means something in the context of what was just
# proposed, so pending intents are stored per conversation and expire --
# a "yeah do it" twenty minutes after the fact almost certainly refers to
# something else, and executing a stale reboot then would be awful.
_PENDING_TTL_SECONDS = 300.0

_AFFIRMATIVE = re.compile(
    r"^\s*(y|ya|yes+|yeah|yep|yup|sure|ok|okay|do it|go|go ahead|please do|"
    r"send it|confirm|confirmed|affirmative|absolutely|definitely)\b[\s.!]*",
    re.IGNORECASE,
)
_NEGATIVE = re.compile(
    r"^\s*(n|no+|nope|nah|don'?t|do not|cancel|stop|never\s*mind|nevermind|abort|wait)\b[\s.!]*",
    re.IGNORECASE,
)


class PendingConfirmations:
    """Tracks the one action awaiting a yes/no, per conversation key.

    Single-slot per conversation on purpose: proposing a second action
    replaces the first, so "yes" can never be ambiguous about which
    thing it's agreeing to."""

    def __init__(self, ttl_seconds: float = _PENDING_TTL_SECONDS):
        self._ttl = ttl_seconds
        self._pending: dict[str, tuple[Intent, float]] = {}

    def put(self, key: str, intent: Intent) -> None:
        self._pending[key] = (intent, time.time())

    def get(self, key: str) -> Optional[Intent]:
        entry = self._pending.get(key)
        if entry is None:
            return None
        intent, stamp = entry
        if time.time() - stamp > self._ttl:
            self._pending.pop(key, None)
            return None
        return intent

    def clear(self, key: str) -> None:
        self._pending.pop(key, None)


def is_affirmative(message: str) -> bool:
    return bool(_AFFIRMATIVE.match(message or ""))


def is_negative(message: str) -> bool:
    return bool(_NEGATIVE.match(message or ""))


# ------------------------------------------------------------ path finding

# Where user scripts/configs actually live. Ordered by how likely a
# "fix my X" refers to something there.
_SEARCH_ROOTS = ["~/bin", "~/scripts", "~/Downloads", "~/Documents", "~/.config", "~"]

_SKIP_DIRS = {
    ".git", "node_modules", "__pycache__", ".venv", "venv", ".cache",
    ".gradle", "build", "dist", ".mozilla", ".steam", "snap",
}

_MAX_SCAN_ENTRIES = 20000

# Filler words people naturally say that carry no filename signal --
# "fix the router" and "my backup script" should match router.py and
# backup.sh, so these get dropped before matching rather than being
# required to appear in the filename.
_HINT_STOPWORDS = {
    "the", "my", "a", "an", "that", "this", "some", "our", "your",
    "file", "files", "script", "scripts", "config", "program",
}


def _hint_words(hint_stem: str) -> list[str]:
    return [
        w for w in re.split(r"[\s_\-.]+", hint_stem)
        if len(w) > 2 and w not in _HINT_STOPWORDS
    ]


def _candidate_score(filename: str, hint: str) -> float:
    """Higher is better. 0 means not a match at all."""
    f = filename.lower()
    h = hint.lower().strip()
    if not h:
        return 0.0
    if f == h:
        return 100.0
    stem = Path(f).stem
    hint_stem = Path(h).stem
    if stem == hint_stem:
        return 90.0
    if h in f:
        return 70.0
    if hint_stem and hint_stem in stem:
        return 60.0
    # Every meaningful word in the hint appearing somewhere in the
    # filename -- catches "backup script" -> backup.sh, "the router" ->
    # router.py.
    words = _hint_words(hint_stem)
    if words and all(w in stem for w in words):
        return 50.0
    return 0.0


def find_file(hint: str, project_root: str = ".", extra_roots: Optional[list[str]] = None) -> list[str]:
    """Find files matching a loose description, best match first.

    This is what makes "fix my backup script" work without a path. Scans
    the project first (most likely target), then the usual home dirs,
    skipping the noisy directories that would otherwise dominate a home
    scan. Capped at _MAX_SCAN_ENTRIES so a huge home directory can't
    turn one chat message into a multi-second filesystem crawl."""
    hint = (hint or "").strip()
    if not hint:
        return []

    # An actual path that exists -- nothing to search for.
    direct = Path(hint).expanduser()
    if direct.exists() and direct.is_file():
        return [str(direct.resolve())]

    roots: list[Path] = [Path(project_root).expanduser().resolve()]
    for r in (extra_roots or []) + _SEARCH_ROOTS:
        p = Path(r).expanduser()
        if p.exists() and p.is_dir():
            roots.append(p.resolve())

    seen: set[str] = set()
    scored: list[tuple[float, str]] = []
    scanned = 0

    for root in roots:
        if scanned >= _MAX_SCAN_ENTRIES:
            break
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = [d for d in dirnames if d not in _SKIP_DIRS and not d.startswith(".")]
            for name in filenames:
                scanned += 1
                if scanned >= _MAX_SCAN_ENTRIES:
                    break
                score = _candidate_score(name, hint)
                if score <= 0:
                    continue
                full = str(Path(dirpath) / name)
                if full in seen:
                    continue
                seen.add(full)
                scored.append((score, full))
            if scanned >= _MAX_SCAN_ENTRIES:
                break

    scored.sort(key=lambda x: (-x[0], len(x[1])))
    return [path for _score, path in scored[:10]]


def resolve_file_argument(intent: Intent, project_root: str = ".") -> tuple[Optional[str], list[str]]:
    """Turn a script_fix intent's fuzzy file_hint into a real path.

    Returns (resolved_path, candidates). resolved_path is None when
    there's nothing matching or when it's genuinely ambiguous -- in
    which case the caller should ask rather than guess, since editing
    the wrong file is exactly the failure mode this is meant to avoid."""
    hint = str(intent.args.get("file_hint") or "").strip()
    if not hint:
        return None, []

    candidates = find_file(hint, project_root=project_root)
    if not candidates:
        return None, []
    if len(candidates) == 1:
        return candidates[0], candidates

    # Clear winner (exact/stem match beats everything else) -> take it.
    top_name = Path(candidates[0]).name
    second_name = Path(candidates[1]).name
    if _candidate_score(top_name, hint) > _candidate_score(second_name, hint):
        return candidates[0], candidates

    return None, candidates
