"""A small starter set of skill cards -- the procedures a good engineer
runs almost without thinking, distilled from how the strong coding
harnesses (Aider, SWE-agent, Claude's own skill patterns) actually work.

They seed `data/skills/` as `candidate`s: they're loaded into battles
from the start and earn `active` the normal way, by winning. `/skill
seed` (idempotent -- skips names that already exist).
"""
from __future__ import annotations

from .types import Skill

_SEED = [
    dict(
        name="read-the-error-first",
        purpose="a failing command tells you what's wrong -- read it before touching code",
        trigger_when="a build, test, or command has just failed",
        trigger_matcher=r"fail|error|exception|traceback|exit [1-9]",
        procedure=[
            "read the actual error message and the file:line it names, not the surrounding output",
            "form a one-sentence hypothesis about the cause",
            "only then look at the code that hypothesis points to",
        ],
    ),
    dict(
        name="reproduce-before-fixing",
        purpose="confirm you can see the bug before you try to fix it",
        trigger_when="asked to fix a bug or a failing test",
        trigger_matcher=r"fix|bug|failing|broken|doesn't work|not working",
        procedure=[
            "run the failing test or the exact case that shows the bug",
            "confirm you see the failure and note what it actually says",
            "make the fix, then re-run the same case -- it must now pass",
        ],
    ),
    dict(
        name="one-change-at-a-time",
        purpose="a small model loses the thread on multi-file edits -- go one step at a time",
        trigger_when="a task needs more than one edit",
        procedure=[
            "make the single smallest edit that could move things forward",
            "run the check (tests / compile / lint) to see if it helped",
            "only make the next edit once you know the last one's effect",
        ],
    ),
    dict(
        name="search-before-you-guess",
        purpose="don't assume where a symbol lives or how it's used -- look",
        trigger_when="you need to change or call something defined elsewhere",
        trigger_matcher=r"import|call|use|where is|defined|function|class|method",
        procedure=[
            "grep / repo_map for the name to find every definition and use",
            "read the definition and one real call site before editing",
        ],
    ),
    dict(
        name="verify-before-done",
        purpose="never say DONE on code work without a green check",
        trigger_when="you think the task is finished",
        trigger_matcher=r"fix|bug|implement|add|change|test",
        procedure=[
            "run the task's tests (or compile / lint if there are none)",
            "if red, read the error and iterate",
            "say DONE only when the check is green",
        ],
    ),
    dict(
        name="edit-file-over-rewrite",
        purpose="rewriting a whole file is where a 7B mangles it -- use a targeted edit",
        trigger_when="changing a few lines of an existing file",
        procedure=[
            "read the exact block you want to change",
            "use edit_file with that block verbatim as `search`",
            "keep write_file for creating a new file or a total rewrite",
        ],
    ),
    dict(
        name="snapshot-before-risky-change",
        purpose="anything hard to undo gets a rollback point first",
        trigger_when="about to change a system config, run a package op, or edit outside a git repo",
        trigger_matcher=r"config|/etc|pacman|systemctl|install|upgrade|sysctl|firewall",
        procedure=[
            "take a BTRFS snapshot (snapper) or a git commit of the current state",
            "make the change",
            "note how to roll back if it goes wrong",
        ],
    ),
    dict(
        name="service-status-then-logs",
        purpose="the fix for a broken service is in its logs, not in guessing",
        trigger_when="a systemd service or container is misbehaving",
        trigger_matcher=r"service|systemd|jellyfin|docker|container|daemon|not running|crashed",
        procedure=[
            "systemctl --user status <service>  (or docker ps -a) for the state",
            "journalctl --user -u <service> -n 50  (or docker logs) for the real error",
            "act on what the log says, not on a guess",
        ],
    ),
]


def cards() -> list[Skill]:
    out = []
    for i, s in enumerate(_SEED):
        out.append(Skill(
            id=f"seed_{i:02d}", name=s["name"], purpose=s["purpose"],
            trigger_when=s["trigger_when"], trigger_matcher=s.get("trigger_matcher"),
            procedure=s["procedure"], provenance=["seed"], status="candidate",
        ))
    return out


def seed(project_root: str) -> list[str]:
    """Write any seed card not already present. Returns the names added."""
    from .store import Store
    store = Store(project_root)
    existing = {s.name for s in store.read_skills()}
    to_add = [c for c in cards() if c.name not in existing]
    if to_add:
        store.write_skills(store.read_skills() + to_add)
    return [c.name for c in to_add]
