"""Magic ToolHost: shell + file read/write, no sandbox.

"No sandbox" is literal -- commands run as the current user. The only
containment is a path jail: every path argument is resolved and must stay
inside the battle's working directory (a throwaway copy of the target
repo, made fresh per battle by campaign.py). A real sandbox is a harness
concern the design doc leaves to whoever deploys Magic.
"""
from __future__ import annotations

import os
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path


@dataclass
class ToolCall:
    name: str
    args: dict


@dataclass
class ToolResult:
    ok: bool
    output: str


def _precheck(path: str, text: str) -> str:
    """Return a rejection message if `text` is obviously broken for its
    file type, else "". Cheap static checks only -- syntax, not logic."""
    if path.endswith(".py"):
        try:
            compile(text, path, "exec")
        except SyntaxError as e:
            return f"SyntaxError: {e.msg} (line {e.lineno})"
    elif path.endswith(".json"):
        import json
        try:
            json.loads(text)
        except ValueError as e:
            return f"invalid JSON: {e}"
    return ""


class ShellToolHost:
    TOOLS = {
        "run_shell":  "run_shell(cmd)                  -- run a shell command in the repo root",
        "read_file":  "read_file(path)                 -- print a file's contents",
        "edit_file":  "edit_file(path, search, replace) -- replace the first exact match of `search` "
                      "(prefer this over write_file for an existing file)",
        "write_file": "write_file(path, text)          -- overwrite a whole file with text",
        "list_dir":   "list_dir(path)                  -- list a directory (defaults to '.')",
    }

    def __init__(self, root: str | Path, shell_timeout: int = 60, max_output: int = 8000):
        self.root = Path(root).resolve()
        self.shell_timeout = shell_timeout
        self.max_output = max_output
        # Put the interpreter running Einherjar first on PATH so the agent's
        # `pytest` / `python` resolve to the env that actually has the test
        # deps -- otherwise a bare shell has neither and the agent can't
        # check its own work (seen on the first real run).
        self._env = dict(os.environ)
        self._env["PATH"] = str(Path(sys.executable).parent) + os.pathsep + self._env.get("PATH", "")

    def tool_help(self) -> str:
        return "\n".join(f"  {v}" for v in self.TOOLS.values())

    # -- path jail ---------------------------------------------------

    def _resolve(self, rel: str) -> Path | None:
        p = (self.root / (rel or ".")).resolve()
        if p == self.root or self.root in p.parents:
            return p
        return None

    def _clip(self, text: str) -> str:
        if len(text) <= self.max_output:
            return text
        head = text[: self.max_output // 2]
        tail = text[-self.max_output // 2:]
        return f"{head}\n...[{len(text) - self.max_output} chars clipped]...\n{tail}"

    # -- dispatch --------------------------------------------------

    def run(self, call: ToolCall) -> ToolResult:
        fn = getattr(self, f"_t_{call.name}", None)
        if fn is None:
            return ToolResult(False, f"unknown tool {call.name!r}. available: {', '.join(self.TOOLS)}")
        try:
            return fn(call.args)
        except Exception as e:  # a tool blowing up is a battle event, not a crash
            return ToolResult(False, f"{type(e).__name__}: {e}")

    def _t_run_shell(self, args: dict) -> ToolResult:
        cmd = args.get("cmd") or args.get("command") or ""
        if not cmd:
            return ToolResult(False, "run_shell needs a 'cmd'")
        proc = subprocess.run(
            cmd, shell=True, cwd=self.root, capture_output=True, text=True,
            timeout=self.shell_timeout, env=self._env,
        )
        body = (proc.stdout or "") + (proc.stderr or "")
        return ToolResult(proc.returncode == 0, self._clip(f"[exit {proc.returncode}]\n{body}"))

    def _t_read_file(self, args: dict) -> ToolResult:
        p = self._resolve(args.get("path", ""))
        if p is None:
            return ToolResult(False, "path escapes the repo root")
        if not p.is_file():
            return ToolResult(False, f"no such file: {args.get('path')}")
        return ToolResult(True, self._clip(p.read_text()))

    def _t_write_file(self, args: dict) -> ToolResult:
        p = self._resolve(args.get("path", ""))
        if p is None:
            return ToolResult(False, "path escapes the repo root")
        text = args.get("text", args.get("content", ""))
        # Parse-before-apply (MAGIC.md section 8, from SWE-agent's ACI): a
        # Python file that won't compile never lands -- the model gets the
        # SyntaxError back and fixes it instead of wasting the next few
        # turns discovering the break by running the tests.
        rej = _precheck(str(p), text)
        if rej:
            return ToolResult(False, f"NOT WRITTEN -- {rej}\nFix the syntax and send write_file again.")
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text)
        return ToolResult(True, f"wrote {len(text)} chars to {args.get('path')}")

    def _t_edit_file(self, args: dict) -> ToolResult:
        p = self._resolve(args.get("path", ""))
        if p is None:
            return ToolResult(False, "path escapes the repo root")
        if not p.is_file():
            return ToolResult(False, f"no such file: {args.get('path')} (use write_file to create it)")
        search = args.get("search", args.get("old", ""))
        replace = args.get("replace", args.get("new", ""))
        if not search:
            return ToolResult(False, "edit_file needs a non-empty 'search'")
        original = p.read_text()
        if search in original:
            updated = original.replace(search, replace, 1)
        else:
            # whitespace-flexible fallback: match ignoring leading indent
            norm = lambda s: "\n".join(line.strip() for line in s.splitlines())
            lines = original.splitlines(keepends=True)
            target = norm(search)
            hit = None
            for i in range(len(lines)):
                for j in range(i + 1, len(lines) + 1):
                    if norm("".join(lines[i:j])) == target:
                        hit = (i, j)
                        break
                if hit:
                    break
            if not hit:
                return ToolResult(False, "search text not found (exact or whitespace-insensitive). "
                                         "read_file first and copy the block verbatim.")
            i, j = hit
            updated = "".join(lines[:i]) + replace + ("" if replace.endswith("\n") else "\n") + "".join(lines[j:])
        rej = _precheck(str(p), updated)
        if rej:
            return ToolResult(False, f"NOT WRITTEN -- edit would break the file: {rej}")
        p.write_text(updated)
        return ToolResult(True, f"edited {args.get('path')} ({len(original)} -> {len(updated)} chars)")

    def _t_list_dir(self, args: dict) -> ToolResult:
        p = self._resolve(args.get("path", "."))
        if p is None:
            return ToolResult(False, "path escapes the repo root")
        if not p.is_dir():
            return ToolResult(False, f"not a directory: {args.get('path')}")
        entries = sorted(
            (c.name + ("/" if c.is_dir() else "")) for c in p.iterdir()
        )
        return ToolResult(True, "\n".join(entries) or "(empty)")
