"""Truth Engine step 2 (proposal N8): a receipt for every finished task.

When Magic finishes something (restarts a container, runs a command,
updates the system, edits code) the reply ends with a short receipt:

  Receipt: worked, and I checked
  - Did: Restarted jellyfin
  - Proof: `docker restart jellyfin` exited 0
  - Checked: right after, it shows "Up 4 seconds"
  - Not checked: whether it stays up (I looked once)

The point is that "done" never has to be taken on faith: the receipt
separates what Magic claims, the evidence it has, what it actually
checked afterwards, and what it doesn't know. The builders are pure (no
I/O), so the wording is testable; the tool handlers in tools.py gather
the evidence and call them.

The receipt text also lands in the conversation history, so a later
"did that work?" is answered from the receipt instead of from memory.
"""
from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
from pathlib import PurePath
from typing import Optional

# Same words as magic/verdict.py, plus REPORTED: the tool itself said it
# worked (exit code 0) but nothing checked the result afterwards. That is
# the most common honest state and it must not read as "verified".
VERIFIED = "verified"
REPORTED = "reported"
PARTIAL = "partial"
INCONCLUSIVE = "inconclusive"
FAILED = "failed"

STATUS_TEXT = {
    VERIFIED: "worked, and I checked",
    REPORTED: "says it worked, not double-checked",
    PARTIAL: "partly checked",
    INCONCLUSIVE: "can't confirm yet",
    FAILED: "didn't work",
}


@dataclass
class Receipt:
    claim: str
    status: str
    evidence: list[str] = field(default_factory=list)
    tested: list[str] = field(default_factory=list)
    unknown: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)

    def render(self) -> str:
        """Plain text: the app shows replies as plain text, not markdown."""
        lines = [f"Receipt: {STATUS_TEXT.get(self.status, self.status)}",
                 f"- Did: {self.claim}"]
        lines += [f"- Proof: {e}" for e in self.evidence]
        if self.tested:
            lines += [f"- Checked: {t}" for t in self.tested]
        elif self.status != FAILED:
            lines.append("- Checked: nothing after it ran")
        lines += [f"- Not checked: {u}" for u in self.unknown]
        return "\n".join(lines)


def attach(result: dict, receipt: Optional[Receipt]) -> dict:
    """Add the receipt to a tool result: rendered at the end of the
    answer, and as data under "receipt" for the app and the log."""
    if receipt is None:
        return result
    answer = (result.get("answer") or "").rstrip()
    result["answer"] = (answer + "\n\n" if answer else "") + receipt.render()
    result["receipt"] = receipt.to_dict()
    return result


def _short(text: str, n: int = 160) -> str:
    """First non-empty line, trimmed to n chars."""
    for line in (text or "").splitlines():
        line = line.strip()
        if line:
            return line if len(line) <= n else line[: n - 3] + "..."
    return ""


def pytest_summary(stdout: str) -> str:
    """'554 passed in 41.2s' from pytest's last summary line, or ''."""
    for line in reversed((stdout or "").splitlines()):
        if re.search(r"\b\d+ (passed|failed|errors?|skipped)\b|no tests ran", line):
            return line.strip().strip("=").strip()
    return ""


# ------------------------------------------------------------ commands

def for_command(command: str, exit_code: int, timed_out: bool, stderr: str = "",
                as_root: bool = False) -> Receipt:
    claim = f"Ran `{command}`" + (" as root" if as_root else "")
    if timed_out:
        return Receipt(claim, INCONCLUSIVE,
                       evidence=["it timed out, so it may still be running or only half done"],
                       unknown=["whether it finished"])
    if exit_code == -1:
        why = _short(stderr)
        return Receipt(claim, FAILED,
                       evidence=["it never started" + (f": {why}" if why else "")])
    if exit_code != 0:
        why = _short(stderr)
        return Receipt(claim, FAILED,
                       evidence=[f"exit code {exit_code}" + (f": {why}" if why else "")])
    return Receipt(claim, REPORTED, evidence=["exit code 0"],
                   unknown=["whether it did what you wanted: I only see the exit code and its output"])


# ------------------------------------------------------------ services

_DOCKER_STATE_RE = re.compile(
    r"\b(Up [^\n]*?(?:second|minute|hour|day|week|month)s?(?: \([^)]*\))?"
    r"|Up Less than a second(?: \([^)]*\))?"
    r"|Restarting \(\d+\)[^\n]*?ago"
    r"|Exited \(\d+\)[^\n]*?ago"
    r"|Created|Paused|Dead)")


def service_state(kind: str, name: str, status_output: str) -> tuple[Optional[bool], str]:
    """(running?, the phrase that says so) from `docker ps -a` or
    `systemctl --user status`. running is None when the output doesn't
    say either way."""
    text = status_output or ""
    if kind == "docker":
        rows = [l for l in text.splitlines()
                if re.search(rf"(?<![\w.-]){re.escape(name)}\s*$", l.rstrip())]
        for row in rows:
            m = _DOCKER_STATE_RE.search(row)
            if m:
                phrase = re.sub(r"\s+", " ", m.group(1)).strip()
                return phrase.startswith("Up "), phrase
        return None, ""
    m = re.search(r"Active:\s*(\S+)\s*\(([^)]*)\)", text)
    if not m:
        return None, ""
    phrase = f"{m.group(1)} ({m.group(2)})"
    return m.group(1) == "active" and m.group(2) == "running", phrase


def for_service_restart(name: str, kind: str, command: str, exit_code: int, timed_out: bool,
                        stderr: str, status_ok: Optional[bool], status_output: str) -> Receipt:
    """status_ok/status_output come from the status check run right after
    the restart (status_ok None means it wasn't run)."""
    claim = f"Restarted {name}"
    if timed_out or exit_code != 0:
        why = "timed out" if timed_out else f"exit code {exit_code}"
        detail = _short(stderr)
        return Receipt(claim, FAILED,
                       evidence=[f"`{command}` failed ({why})" + (f": {detail}" if detail else "")])
    evidence = [f"`{command}` exited 0"]
    if not status_ok:
        return Receipt(claim, INCONCLUSIVE, evidence=evidence,
                       unknown=["whether it's running now: the status check after the restart didn't work"])
    running, phrase = service_state(kind, name, status_output)
    if running is None:
        return Receipt(claim, INCONCLUSIVE, evidence=evidence,
                       unknown=["whether it's running now: I couldn't read the status output"])
    if not running:
        return Receipt(claim, FAILED,
                       evidence=evidence + [f"but right after, it shows \"{phrase}\""])
    later = ["whether it stays up (I looked once, right after)",
             f"whether {name} itself works, like its web page or API"]
    if "unhealthy" in phrase:
        return Receipt(claim, PARTIAL, evidence=evidence,
                       tested=[f"it's up (\"{phrase}\"), but Docker's health check says unhealthy"],
                       unknown=later)
    tested = [f"right after, it shows \"{phrase}\""]
    if "health: starting" in phrase:
        later.insert(0, "Docker's own health check, which hadn't finished yet")
    return Receipt(claim, VERIFIED, evidence=evidence, tested=tested, unknown=later)


# ------------------------------------------------------------- system

_KERNEL_RE = re.compile(r"(?:upgrading|installing)\s+(linux\d+)(?=[\s.])|\b(linux\d+)-\d[\w.]*-\d", re.I)


def for_updates(exit_code: int, timed_out: bool, stdout: str, stderr: str) -> Receipt:
    claim = "Ran a full system update (pacman -Syu)"
    if timed_out:
        return Receipt(claim, INCONCLUSIVE, evidence=["it timed out partway"],
                       unknown=["which packages got updated before it stopped",
                                "whether pacman left a lock file (/var/lib/pacman/db.lck)"])
    if exit_code != 0:
        detail = _short(stderr) or _short(stdout)
        return Receipt(claim, FAILED,
                       evidence=[f"pacman exited {exit_code}" + (f": {detail}" if detail else "")])
    out = stdout or ""
    if re.search(r"there is nothing to do", out, re.I):
        return Receipt("Checked for system updates: nothing to install", REPORTED,
                       evidence=["pacman said there is nothing to do"])
    evidence = ["pacman exited 0"]
    m = re.search(r"^Packages \((\d+)\)", out, re.M)
    count = int(m.group(1)) if m else len(re.findall(r"^\(\s*\d+/\d+\) upgrading ", out, re.M))
    if count:
        evidence.append(f"{count} package{'s' if count != 1 else ''} updated")
    unknown = ["whether your apps and containers still work: I didn't test any"]
    k = _KERNEL_RE.search(out)
    if k:
        unknown.insert(0, f"the new kernel ({k.group(1) or k.group(2)}) isn't running until you reboot")
    return Receipt(claim, REPORTED, evidence=evidence, unknown=unknown)


def for_reboot(exit_code: int) -> Receipt:
    evidence = (["systemctl accepted the reboot"] if exit_code == 0
                else [f"systemctl returned {exit_code}, which often just means the connection dropped as it went down"])
    return Receipt("Sent the reboot command", INCONCLUSIVE, evidence=evidence,
                   unknown=["whether it came back up: I can't check while it's down. "
                            "If I answer your next message, it's back"])


def for_rollback(number: str, ok: bool, message: str) -> Receipt:
    claim = f"Rolled back to snapshot {number}"
    if ok:
        return Receipt(claim + " and rebooted", INCONCLUSIVE,
                       evidence=["snapper staged the rollback", "the reboot was sent"],
                       unknown=["whether it booted into that snapshot: check once it's back"])
    if "staged successfully" in (message or ""):
        return Receipt(claim, PARTIAL, evidence=["snapper staged the rollback"],
                       unknown=["it isn't active yet: the reboot didn't go through, so reboot to apply it"])
    return Receipt(claim, FAILED, evidence=[_short(message) or "snapper rollback failed"])


# --------------------------------------------------------------- code

def _is_test_file(path: str) -> bool:
    p = PurePath(path)
    return p.name.startswith("test_") or p.name.endswith("_test.py") or "tests" in p.parts


def for_code_change(kind: str, claim: str, files_changed: list[str], checks: Optional[dict],
                    approved_by: Optional[list[str]] = None, extra_evidence: Optional[list[str]] = None,
                    run_hint: str = "") -> Receipt:
    """kind: self_edit | build | script_fix. checks is what the edit
    pipeline recorded: {"compiled": [py files], "tests": "passed" |
    "none" | "not_run", "test_summary": "..."}."""
    checks = checks or {}
    files = list(files_changed or [])
    evidence = list(extra_evidence or [])
    if approved_by:
        evidence.append(f"approved by {' and '.join(approved_by)} before it went in")
    tested, unknown = [], []
    compiled = checks.get("compiled") or []
    if compiled:
        tested.append(f"{len(compiled)} Python file{'s' if len(compiled) != 1 else ''} compile")
    tests = checks.get("tests", "not_run")
    summary = checks.get("test_summary") or ""
    if tests == "passed":
        tested.append("the test suite passed" + (f" ({summary})" if summary else ""))
        if not any(_is_test_file(f) for f in files):
            unknown.append("whether the new part works: no test was added for it, "
                           "so the tests only show nothing else broke")
    elif tests == "none":
        unknown.append("no tests exist here, so nothing ran the code")
    else:
        unknown.append(f"I didn't run {run_hint or 'the code'} or any tests")
    if tests == "passed" and any(_is_test_file(f) for f in files):
        status = VERIFIED
    elif tested:
        status = PARTIAL
    else:
        status = REPORTED
    return Receipt(claim, status, evidence=evidence, tested=tested, unknown=unknown)
