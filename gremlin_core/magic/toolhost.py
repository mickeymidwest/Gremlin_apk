"""Magic ToolHost: shell + file read/write, no sandbox.

"No sandbox" is literal -- commands run as the current user. The only
containment is a path jail: every path argument is resolved and must stay
inside the battle's working directory (a throwaway copy of the target
repo, made fresh per battle by campaign.py). A real sandbox is a harness
concern the design doc leaves to whoever deploys Magic.
"""
from __future__ import annotations

import ast
import os
import re
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
        "repo_map":   "repo_map(query)                 -- symbol-level map of the repo, "
                      "ranked toward `query`; read this before opening files",
        "run_shell":  "run_shell(cmd)                  -- run a shell command in the repo root",
        "read_file":  "read_file(path)                 -- print a file's contents",
        "edit_file":  "edit_file(path, search, replace) -- replace the first exact match of `search` "
                      "(prefer this over write_file for an existing file)",
        "write_file": "write_file(path, text)          -- overwrite a whole file with text",
        "list_dir":   "list_dir(path)                  -- list a directory (defaults to '.')",
    }

    # Phase-gated tool space (MAGIC.md section 8, #2): a small model does
    # much better when it can't edit before it has looked.
    EXPLORE_TOOLS = ("repo_map", "read_file", "list_dir", "run_shell")

    # Shell verbs that can change state -- blocked in read-only mode so
    # /do can answer "what's using my disk" by actually checking, without
    # any risk of it running something destructive.
    _WRITE_VERBS = (
        "rm", "mv", "cp", "dd", "mkfs", "shred", "truncate", ">", ">>", "tee",
        "chmod", "chown", "chattr", "ln", "install", "rsync", "kill", "pkill",
        "systemctl", "reboot", "shutdown", "poweroff", "mount", "umount",
        "pacman", "yay", "pip", "npm", "apt", "docker rm", "docker stop",
        "docker rmi", "docker kill", "git commit", "git push", "git reset",
        "git checkout", "git clean", "curl", "wget", "nc", "ncat",
        # general-purpose interpreters -- a one-liner in any of these
        # sidesteps every verb check above (python -c "os.remove(...)").
        # sed/awk/find stay allowed (read-only text work is their bread
        # and butter) but their write forms are caught by _READONLY_EXTRA.
        "python", "python2", "python3", "perl", "ruby", "node", "php",
    )

    # write paths the plain token scan misses, all blocked in read-only mode:
    #   $(...) / `...` / <(...) / >(...)  -- substitution hides a write verb
    #   ls>f   cat x>>y                   -- redirect with no leading space
    #                                       (a letter/quote/dot before '>',
    #                                        so 2>&1 and 2>/dev/null pass)
    #   sed -i / perl -i / -i''           -- in-place edit
    #   find ... -delete / -exec / -ok / -fprint
    #   bash -c / sh -c / zsh -c          -- nested shell one-liner
    _READONLY_EXTRA = re.compile(
        r"""\$\(|`|<\(|>\("""
        r"""|['"a-zA-Z.]>>?|&>|>\|"""
        r"""|(?:sed|perl)\s(?:[^|;&]*\s)?-i\b"""
        r"""|-delete\b|-exec[a-z]*\s|-ok\s|-fprint\b"""
        r"""|(?:^|[|;&\s])(?:ba|z)?sh\s+-c\b""")

    def __init__(self, root: str | Path, shell_timeout: int = 60, max_output: int = 8000,
                 allowed: "tuple[str, ...] | None" = None, readonly: bool = False):
        self.root = Path(root).resolve()
        self.shell_timeout = shell_timeout
        self.max_output = max_output
        self.allowed = tuple(allowed) if allowed is not None else tuple(self.TOOLS)
        self.readonly = readonly
        if readonly:
            self.allowed = tuple(t for t in self.allowed if t not in ("write_file", "edit_file"))
        # Put the interpreter running Einherjar first on PATH so the agent's
        # `pytest` / `python` resolve to the env that actually has the test
        # deps -- otherwise a bare shell has neither and the agent can't
        # check its own work (seen on the first real run).
        self._env = dict(os.environ)
        self._env["PATH"] = str(Path(sys.executable).parent) + os.pathsep + self._env.get("PATH", "")

    def tool_help(self) -> str:
        return "\n".join(f"  {self.TOOLS[n]}" for n in self.TOOLS if n in self.allowed)

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
            return ToolResult(False, f"unknown tool {call.name!r}. available: {', '.join(self.allowed)}")
        if call.name not in self.allowed:
            return ToolResult(False, f"{call.name} isn't available yet. "
                              f"Right now you can use: {', '.join(self.allowed)}. "
                              "Look at the code first.")
        try:
            return fn(call.args)
        except Exception as e:  # a tool blowing up is a battle event, not a crash
            return ToolResult(False, f"{type(e).__name__}: {e}")

    @staticmethod
    def _val(args: dict, *keys: str) -> str:
        """First non-empty value under any of `keys`, then -- if the model
        passed a single-entry dict under a junk key like {"arg": ...} or
        just copied the protocol's {"value": ...} example -- that lone
        value. Small models get the exact key name wrong constantly."""
        for k in keys:
            v = args.get(k)
            if v:
                return str(v)
        if len(args) == 1:
            only = next(iter(args.values()))
            if isinstance(only, str) and only:
                return only
        return ""

    def _t_run_shell(self, args: dict) -> ToolResult:
        cmd = self._val(args, "cmd", "command", "shell", "run")
        if not cmd:
            return ToolResult(False, "run_shell needs a 'cmd'")
        if self.readonly:
            low = cmd.lower()
            hit = next((v for v in self._WRITE_VERBS
                        if re.search(rf"(^|[|;&\s]){re.escape(v)}([|;&\s]|$)", low)), None)
            if not hit and self._READONLY_EXTRA.search(low):
                hit = "a redirect / substitution / in-place edit"
            if hit:
                return ToolResult(False, f"read-only mode: `{hit}` can change state and is blocked. "
                                         "Use a command that only reads (df, du, ls, ps, cat, ...).")
        proc = subprocess.run(
            cmd, shell=True, cwd=self.root, capture_output=True, text=True,
            timeout=self.shell_timeout, env=self._env,
        )
        body = (proc.stdout or "") + (proc.stderr or "")
        return ToolResult(proc.returncode == 0, self._clip(f"[exit {proc.returncode}]\n{body}"))

    def _t_read_file(self, args: dict) -> ToolResult:
        rel = self._val(args, "path", "file", "filename", "arg", "name")
        p = self._resolve(rel)
        if p is None:
            return ToolResult(False, "path escapes the repo root")
        if not p.is_file():
            return ToolResult(False, f"no such file: {rel or '(empty path)'} -- "
                              "list_dir to see what's there; give read_file a 'path'")
        return ToolResult(True, self._clip(p.read_text()))

    def _t_write_file(self, args: dict) -> ToolResult:
        rel = self._val(args, "path", "file", "filename", "name")
        p = self._resolve(rel)
        if p is None:
            return ToolResult(False, "path escapes the repo root")
        text = args.get("text", args.get("content", args.get("body", "")))
        # Parse-before-apply (MAGIC.md section 8, from SWE-agent's ACI): a
        # Python file that won't compile never lands -- the model gets the
        # SyntaxError back and fixes it instead of wasting the next few
        # turns discovering the break by running the tests.
        rej = _precheck(str(p), text)
        if not rel:
            return ToolResult(False, "write_file needs a 'path' and 'text'")
        if rej:
            return ToolResult(False, f"NOT WRITTEN -- {rej}\nFix the syntax and send write_file again.")
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text)
        return ToolResult(True, f"wrote {len(text)} chars to {rel}")

    def _t_edit_file(self, args: dict) -> ToolResult:
        rel = self._val(args, "path", "file", "filename", "name")
        p = self._resolve(rel)
        if p is None:
            return ToolResult(False, "path escapes the repo root")
        if not p.is_file():
            return ToolResult(False, f"no such file: {rel or '(empty path)'} (use write_file to create it)")
        search = args.get("search", args.get("old", args.get("find", "")))
        replace = args.get("replace", args.get("new", args.get("with", "")))
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
        return ToolResult(True, f"edited {rel} ({len(original)} -> {len(updated)} chars)")

    def unlock_all(self) -> None:
        """Called by battle.py once the agent has actually looked at the
        code -- opens the editing tools."""
        self.allowed = tuple(self.TOOLS)

    _SKIP_MAP_DIRS = {".git", "venv", ".venv", "__pycache__", "node_modules",
                      "build", "dist", ".pytest_cache", ".gradle", ".idea"}

    def _t_repo_map(self, args: dict) -> ToolResult:
        query = self._val(args, "query", "q", "arg", "name").lower()
        qwords = set(re.findall(r"[a-z_]{3,}", query))
        rows: list[tuple[int, str]] = []
        for p in sorted(self.root.rglob("*.py")):
            rel = p.relative_to(self.root)
            if any(part in {".git", "venv", ".venv", "__pycache__", "node_modules",
                            "build", "dist", ".pytest_cache"} for part in rel.parts):
                continue
            try:
                tree = ast.parse(p.read_text())
            except (SyntaxError, UnicodeDecodeError, OSError):
                continue
            doc = (ast.get_docstring(tree) or "").splitlines()
            syms: list[str] = []
            for node in tree.body:
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    a = ", ".join(ar.arg for ar in node.args.args)
                    syms.append(f"def {node.name}({a})")
                elif isinstance(node, ast.ClassDef):
                    meths = [n.name for n in node.body
                             if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]
                    syms.append(f"class {node.name}" + (f"  {{{', '.join(meths[:8])}}}" if meths else ""))
            block = [f"{rel}" + (f"  — {doc[0]}" if doc else "")]
            block += [f"    {s}" for s in syms]
            text = "\n".join(block)
            hay = set(re.findall(r"[a-z_]{3,}", text.lower()))
            score = len(qwords & hay) if qwords else 0
            rows.append((score, text))
        rows.sort(key=lambda r: -r[0])
        top = [t for _, t in rows[:40]]
        if top:
            return ToolResult(True, self._clip("\n".join(top)))
        # not a Python repo -- fall back to a source-file tree + a grep of
        # the query, so repo_map is still useful for Kotlin/JS/Go/etc.
        return self._nonpython_map(query)

    _SRC_EXT = (".kt", ".kts", ".java", ".js", ".ts", ".tsx", ".go", ".rs",
                ".c", ".h", ".cc", ".cpp", ".hpp", ".swift", ".rb", ".sh", ".gradle")

    def _nonpython_map(self, query: str) -> ToolResult:
        files = []
        for p in sorted(self.root.rglob("*")):
            rel = p.relative_to(self.root)
            if any(part in self._SKIP_MAP_DIRS for part in rel.parts):
                continue
            if p.is_file() and (p.suffix in self._SRC_EXT or p.name in (
                    "settings.gradle.kts", "build.gradle.kts", "AndroidManifest.xml",
                    "package.json", "Cargo.toml", "go.mod")):
                try:
                    n = sum(1 for _ in p.open("rb"))
                except OSError:
                    n = 0
                files.append((str(rel), n))
        out = ["source files:"]
        out += [f"  {r}  ({n} lines)" for r, n in files[:60]]
        if query:
            try:
                g = subprocess.run(
                    ["grep", "-rn", "--include=*.kt", "--include=*.java", "--include=*.js",
                     "--include=*.ts", "--include=*.go", "--include=*.rs", query, "."],
                    cwd=self.root, capture_output=True, text=True, timeout=15)
                hits = [ln for ln in (g.stdout or "").splitlines() if ln][:25]
                if hits:
                    out.append(f"\nmatches for {query!r}:")
                    out += [f"  {h}" for h in hits]
            except (OSError, subprocess.TimeoutExpired):
                pass
        return ToolResult(True, self._clip("\n".join(out)))

    def _t_list_dir(self, args: dict) -> ToolResult:
        rel = self._val(args, "path", "dir", "directory", "arg") or "."
        p = self._resolve(rel)
        if p is None:
            return ToolResult(False, "path escapes the repo root")
        if not p.is_dir():
            return ToolResult(False, f"not a directory: {rel}")
        entries = sorted(
            (c.name + ("/" if c.is_dir() else "")) for c in p.iterdir()
        )
        return ToolResult(True, "\n".join(entries) or "(empty)")
