#!/usr/bin/env python3
"""Claude Code PreToolUse guard for Gremlin_apk.

Blocks (exit 2, reason goes back to Claude):
  1. Commands that delete files or discard work.
  2. Heavy build commands not wrapped in scripts/heavy.sh.

Escape hatch (Mickey only): launch claude with GREMLIN_ALLOW_DESTRUCTIVE=1 exported.
"""
import json
import os
import re
import sys

DESTRUCTIVE = [
    (r"(^|[\s;&|(])(?<!git )rm\s", "rm"),
    (r"(^|[\s;&|(])rmdir\s", "rmdir"),
    (r"(^|[\s;&|(])(shred|unlink)\s", "shred/unlink"),
    (r"\bfind\b.*\s-delete\b", "find -delete"),
    (r"\bfind\b.*-exec\s+rm\b", "find -exec rm"),
    (r"\bgit\s+reset\s+.*--hard", "git reset --hard"),
    (r"\bgit\s+clean\b", "git clean"),
    (r"\bgit\s+checkout\s+(\S+\s+)?--\s", "git checkout -- <paths>"),
    (r"\bgit\s+checkout\s+\.(\s|$)", "git checkout ."),
    (r"\bgit\s+restore\b(?!.*--staged)", "git restore (worktree)"),
    (r"\bgit\s+stash\s+(drop|clear)\b", "git stash drop/clear"),
    (r"\bgit\s+branch\s+.*-D\b", "git branch -D"),
    (r"\bgit\s+push\s+.*(--force|\s-f\b|--force-with-lease|\s\+\S)", "git push --force"),
    (r"\bgit\s+rm\b(?!.*--cached)", "git rm"),
    (r"(^|[\s;&|(])truncate\s", "truncate"),
]

DEFAULT_HEAVY = (r"(\./gradlew|(^|[[:space:];&|])gradle[[:space:]]|(^|[[:space:];&|])make([[:space:]]|$)"
                 r"|docker[[:space:]]+(build|compose[[:space:]]+build)|npm[[:space:]]+run[[:space:]]+build)")


def heavy_regex(root):
    pat = DEFAULT_HEAVY
    try:
        with open(os.path.join(root, ".gremlin", "daemon.env")) as f:
            for line in f:
                m = re.match(r"\s*HEAVY_REGEX=(['\"]?)(.*)\1\s*$", line)
                if m:
                    pat = m.group(2)
    except OSError:
        pass
    return re.compile(pat.replace("[:space:]", r"\s"))


def banned_rules(root):
    rules = []
    try:
        with open(os.path.join(root, ".gremlin", "banned.txt")) as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                pat, _, why = line.partition("||")
                try:
                    rules.append((re.compile(pat.strip()), why.strip() or "banned in .gremlin/banned.txt"))
                except re.error:
                    pass
    except OSError:
        pass
    return rules


def block(msg):
    print(msg, file=sys.stderr)
    sys.exit(2)


def main():
    try:
        data = json.load(sys.stdin)
    except Exception:
        return 0
    if data.get("tool_name") != "Bash":
        return 0
    cmd = (data.get("tool_input") or {}).get("command", "") or ""
    root = os.environ.get("CLAUDE_PROJECT_DIR") or data.get("cwd") or os.getcwd()

    for rx, why in banned_rules(root):
        if rx.search(cmd):
            block(
                f"BLOCKED by Gremlin guard: banned method ({why}).\n"
                "This approach was abandoned because it crashed the machine. Per CALIBRATION.md §8, keep "
                "iterating on the CURRENT method (tune its settings, check .gremlin/runs.md). "
                "Don't bring the banned method back in any form."
            )

    if os.environ.get("GREMLIN_ALLOW_DESTRUCTIVE") != "1":
        for pat, name in DESTRUCTIVE:
            if re.search(pat, cmd):
                block(
                    f"BLOCKED by Gremlin guard: '{name}' can delete or discard work.\n"
                    "Per CALIBRATION.md: move unwanted files to .gremlin/trash/ (mkdir -p .gremlin/trash && mv <file> .gremlin/trash/), "
                    "or stop and ask Mickey. Do not try to work around this block."
                )

    if heavy_regex(root).search(cmd) and "scripts/heavy.sh" not in cmd:
        block(
            "BLOCKED by Gremlin guard: heavy command must run with the daemon paused.\n"
            f"Re-run it as:  scripts/heavy.sh {cmd.strip()}"
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
