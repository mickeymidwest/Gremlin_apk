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


_DECL_RE = re.compile(r"\b(def|fun|fn|function|class|struct|sub|impl|interface)\b")


def _leading_ws(s: str) -> str:
    return s[: len(s) - len(s.lstrip())]


def _reindent(text: str, want: str, have: str) -> str:
    """Shift every line of `text` so its base indent goes from `have` to
    `want` (keeps relative nesting)."""
    if want == have:
        return text
    out = []
    for ln in text.splitlines(keepends=True):
        stripped = ln.lstrip(" \t")
        if not stripped.strip():
            out.append(ln)
            continue
        cur = ln[: len(ln) - len(stripped)]
        rel = cur[len(have):] if cur.startswith(have) else cur
        out.append(want + rel + stripped)
    return "".join(out)


def _block_end(lines: list[str], i: int) -> int:
    """Line index one past the end of the def/class block that starts at
    line i -- brace-balanced if it opens with '{', else by indentation."""
    if lines[i].rstrip().endswith("{"):
        depth = 0
        for k in range(i, len(lines)):
            depth += lines[k].count("{") - lines[k].count("}")
            if k > i and depth <= 0:
                return k + 1
        return len(lines)
    base = len(lines[i]) - len(lines[i].lstrip())
    k = i + 1
    while k < len(lines):
        s = lines[k]
        if s.strip() and (len(s) - len(s.lstrip())) <= base:
            break
        k += 1
    return k


def _edit_locate(original: str, search: str, replace: str):
    """Find the [i, j) line span in `original` that `search` refers to.
    Returns (i, j), or a str error message with nearby lines. Handles a
    near-miss single-line search (paraphrased signature, dropped type
    hint) and, when `replace` is a whole method and `search` is just its
    header line, extends the span over the old body."""
    import difflib
    lines = original.splitlines(keepends=True)
    norm = lambda s: "\n".join(ln.strip() for ln in s.splitlines())
    s_lines = search.strip("\n").splitlines() or [search]

    def _line_span_of_substring(sub: str):
        pos = original.find(sub)
        if pos < 0:
            return None
        i = original.count("\n", 0, pos)
        j = i + sub.count("\n") + 1
        # only accept if it covers those lines whole (not a mid-line splice)
        if norm("".join(lines[i:j])) == norm(sub):
            return (i, j)
        return None

    anchor = None
    if len(s_lines) > 1:
        anchor = _line_span_of_substring(search)
        if anchor is None:                                   # whitespace-flex block
            tgt = norm(search)
            for i in range(len(lines)):
                for j in range(i + 1, min(len(lines), i + len(s_lines) + 3) + 1):
                    if norm("".join(lines[i:j])) == tgt:
                        anchor = (i, j)
                        break
                if anchor:
                    break
    else:
        one = search.strip()
        exact = [k for k, ln in enumerate(lines) if ln.strip() == one]
        if len(exact) == 1:
            anchor = (exact[0], exact[0] + 1)
        elif len(exact) > 1:
            ns = ", ".join(str(k + 1) for k in exact)
            return (f"'{one}' appears on {len(exact)} lines ({ns}) -- ambiguous. Give a "
                    "multi-line 'search' with a unique line above/below it, or use write_file "
                    "to rewrite the whole file.")
        if anchor is None:                       # signature-name match
            m = re.search(r"\b([A-Za-z_]\w*)\s*\(", one)
            if m:
                nm = m.group(1)
                decl = [k for k, ln in enumerate(lines)
                        if _DECL_RE.search(ln) and re.search(rf"\b{re.escape(nm)}\s*\(", ln)]
                if len(decl) > 1:
                    ns = ", ".join(str(k + 1) for k in decl)
                    return (f"more than one definition names '{nm}' (lines {ns}) -- your search "
                            "matches all of them. Rewrite the whole file with write_file, or "
                            "give a multi-line search unique to the one you mean.")
                if len(decl) == 1:
                    anchor = (decl[0], decl[0] + 1)
        if anchor is None and len(one) >= 12:    # fuzzy single line
            best_r, best_k = 0.0, None
            for k, ln in enumerate(lines):
                r = difflib.SequenceMatcher(None, one, ln.strip()).ratio()
                if r > best_r:
                    best_r, best_k = r, k
            if best_k is not None and best_r >= 0.8:
                anchor = (best_k, best_k + 1)

    if anchor is None:
        # show the closest few lines so the model can correct
        best_r, best_k = 0.0, 0
        for k, ln in enumerate(lines):
            r = difflib.SequenceMatcher(None, norm(search), ln.strip()).ratio()
            if r > best_r:
                best_r, best_k = r, k
        lo, hi = max(0, best_k - 1), min(len(lines), best_k + 3)
        near = "".join(lines[lo:hi]).rstrip()
        return ("search text not found -- copy an exact snippet from read_file, "
                f"or use write_file for a whole-file change.\nclosest lines:\n{near}")

    i, j = anchor
    # header-only search + multi-line replace -> replace the whole block
    if (j - i == 1 and "\n" in replace.strip() and _DECL_RE.search(lines[i])):
        j = _block_end(lines, i)
    return (i, j)


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
        "repo_map":   "repo_map(query)                 -- map of the repo, ranked toward "
                      "`query`; read this before opening files",
        "grep":       "grep(pattern, path)             -- search files for a regex "
                      "(path defaults to '.'); use this to find where something is defined/used",
        "run_shell":  "run_shell(cmd)                  -- run a shell command in the repo root",
        "read_file":  "read_file(path)                 -- print a file's contents",
        "view_file":  "view_file(path, start, count)   -- print `count` lines of a file from line "
                      "`start`, with line numbers (for navigating a big file)",
        "edit_file":  "edit_file(path, search, replace) -- replace the first exact match of `search` "
                      "(prefer this over write_file for an existing file)",
        "write_file": "write_file(path, text)          -- overwrite a whole file with text",
        "undo_last":  "undo_last()                     -- revert your most recent edit "
                      "(rolls back the last per-step snapshot)",
        "list_dir":   "list_dir(path)                  -- list a directory (defaults to '.')",
    }

    # Phase-gated tool space (MAGIC.md section 8, #2): a small model does
    # much better when it can't edit before it has looked.
    EXPLORE_TOOLS = ("repo_map", "grep", "read_file", "view_file", "list_dir", "run_shell")

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
                 allowed: "tuple[str, ...] | None" = None, readonly: bool = False,
                 protect_glob: str | None = None):
        self.root = Path(root).resolve()
        self.shell_timeout = shell_timeout
        self.max_output = max_output
        self.allowed = tuple(allowed) if allowed is not None else tuple(self.TOOLS)
        self.readonly = readonly
        # paths (relative-glob) the agent may READ but not write -- e.g. the
        # target-under-test in a fuzzing battle, which must keep its bug
        self.protect_glob = protect_glob
        if readonly:
            self.allowed = tuple(t for t in self.allowed
                                 if t not in ("write_file", "edit_file", "undo_last"))
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

    def _protected(self, rel: str) -> bool:
        if not self.protect_glob or not rel:
            return False
        import fnmatch
        r = rel.lstrip("./")
        return any(fnmatch.fnmatch(r, g.strip()) for g in self.protect_glob.split(","))

    def _clip(self, text: str) -> str:
        if len(text) <= self.max_output:
            return text
        head = text[: self.max_output // 2]
        tail = text[-self.max_output // 2:]
        return f"{head}\n...[{len(text) - self.max_output} chars clipped]...\n{tail}"

    # -- dispatch --------------------------------------------------

    # common names models reach for that map onto a real tool
    _TOOL_ALIASES = {
        "run_python": "run_shell", "python": "run_shell", "bash": "run_shell",
        "shell": "run_shell", "execute": "run_shell", "run": "run_shell",
        "cat": "read_file", "open": "read_file", "view": "read_file",
        "ls": "list_dir", "search": "grep", "find": "grep",
        "create_file": "write_file", "new_file": "write_file",
        "replace": "edit_file", "patch": "edit_file", "modify": "edit_file",
    }

    def run(self, call: ToolCall) -> ToolResult:
        name = self._TOOL_ALIASES.get(call.name, call.name)
        if name != call.name:
            call = ToolCall(name=name, args=call.args)
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
        # models shorten "python -m pytest" to bare "pytest" (not on PATH
        # in the sandbox env) -- run it through the interpreter that has it
        cmd = re.sub(r"^(\s*)pytest(\s|$)", rf"\1{sys.executable} -m pytest\2", cmd)
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

    def _t_view_file(self, args: dict) -> ToolResult:
        rel = self._val(args, "path", "file", "filename", "name")
        p = self._resolve(rel)
        if p is None or not p.is_file():
            return ToolResult(False, f"no such file: {rel or '(empty path)'}")
        lines = p.read_text().splitlines()
        try:
            start = max(1, int(args.get("start", 1)))
        except (TypeError, ValueError):
            start = 1
        try:
            count = int(args.get("count", args.get("n", 60)))
        except (TypeError, ValueError):
            count = 60
        count = max(1, min(count, 200))
        chunk = lines[start - 1:start - 1 + count]
        w = len(str(start + len(chunk)))
        body = "\n".join(f"{start + i:>{w}}  {ln}" for i, ln in enumerate(chunk))
        tail = "" if start - 1 + count >= len(lines) else f"\n... ({len(lines) - (start-1+count)} more lines)"
        return ToolResult(True, self._clip(f"{rel} lines {start}-{start+len(chunk)-1} of {len(lines)}:\n{body}{tail}"))

    def _t_grep(self, args: dict) -> ToolResult:
        pat = self._val(args, "pattern", "query", "regex", "search", "arg", "q")
        if not pat:
            return ToolResult(False, "grep needs a 'pattern'")
        where = self._val(args, "path", "dir", "in") or "."
        target = self._resolve(where)
        if target is None:
            return ToolResult(False, "path escapes the repo root")
        try:
            proc = subprocess.run(
                ["grep", "-rniI", "--line-number",
                 "--exclude-dir=.git", "--exclude-dir=node_modules",
                 "--exclude-dir=build", "--exclude-dir=.gradle", "--exclude-dir=__pycache__",
                 "-e", pat, "."],
                cwd=target if target.is_dir() else target.parent,
                capture_output=True, text=True, timeout=20)
        except (OSError, subprocess.TimeoutExpired) as e:
            return ToolResult(False, f"grep failed: {e}")
        hits = [ln for ln in (proc.stdout or "").splitlines() if ln]
        if not hits:
            return ToolResult(True, f"no matches for {pat!r}")
        return ToolResult(True, self._clip(f"{len(hits)} match(es) for {pat!r}:\n"
                                           + "\n".join(hits[:60])))

    def _t_write_file(self, args: dict) -> ToolResult:
        rel = self._val(args, "path", "file", "filename", "name")
        p = self._resolve(rel)
        if p is None:
            return ToolResult(False, "path escapes the repo root")
        if self._protected(rel):
            return ToolResult(False, f"{rel} is READ-ONLY for this task -- you can read it but "
                                     "not change it. Put your changes in a different (new) file.")
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
        if self._protected(rel):
            return ToolResult(False, f"{rel} is READ-ONLY for this task -- do not change it. "
                                     "Put your work in a new file in the repo root.")
        if not p.is_file():
            return ToolResult(False, f"no such file: {rel or '(empty path)'} (use write_file to create it)")
        search = args.get("search", args.get("old", args.get("find", "")))
        replace = args.get("replace", args.get("new", args.get("with", "")))
        original = p.read_text()
        if not search:
            if replace:   # empty search + real replace = prepend
                updated = replace + ("" if replace.endswith("\n") else "\n") + original
                rej = _precheck(str(p), updated)
                if rej:
                    return ToolResult(False, f"NOT WRITTEN -- prepend would break the file: {rej}")
                p.write_text(updated)
                return ToolResult(True, f"prepended to {rel} ({len(original)} -> {len(updated)} chars)")
            return ToolResult(False, "edit_file needs 'search' (an exact snippet to replace) and "
                                     "'replace'. To add text at the top, pass an empty 'search' with "
                                     "your new text in 'replace'. To rewrite the whole file, use write_file.")

        # Plain substring replace when the match is unambiguous and won't
        # orphan a block: exact substring, and either the replace is a
        # simple in-line swap or the search already spans whole lines.
        _one_line_header = ("\n" not in search and _DECL_RE.search(search)
                            and search.rstrip().endswith(("{", ":")))
        if search in original and not ("\n" in replace.strip() and _one_line_header):
            updated = original.replace(search, replace, 1)
            verb = "edited"
        else:
            span = _edit_locate(original, search, replace)
            if isinstance(span, str):
                return ToolResult(False, span)   # a helpful "not found" message
            i, j = span
            lines = original.splitlines(keepends=True)
            block = "".join(lines[i:j])
            updated = ("".join(lines[:i]) + replace
                       + ("" if replace.endswith("\n") or not replace else "\n")
                       + "".join(lines[j:]))
            # If the replace's own indentation doesn't line up with the code
            # it's replacing (model dropped/added the method indent), re-align
            # the whole block to the anchor line's indent and retry.
            if _precheck(str(p), updated):
                fixed = _reindent(replace, _leading_ws(lines[i]) if i < len(lines) else "",
                                  _leading_ws(replace))
                if fixed != replace:
                    alt = ("".join(lines[:i]) + fixed
                           + ("" if fixed.endswith("\n") else "\n") + "".join(lines[j:]))
                    if not _precheck(str(p), alt):
                        updated = alt
            verb = "edited" if j - i <= search.count("\n") + 1 else "replaced the block at"
        rej = _precheck(str(p), updated)
        if rej:
            return ToolResult(False,
                f"Your edit to {rel} was NOT applied -- it would make the file invalid "
                f"({rej}). The file is UNCHANGED. Your 'replace' text must be a complete, "
                "correctly-indented drop-in for what 'search' matched (e.g. if search is a "
                "method header, replace is the whole method with its body).")
        p.write_text(updated)
        return ToolResult(True, f"{verb} {rel} ({len(original)} -> {len(updated)} chars)")

    def unlock_all(self) -> None:
        """Called by battle.py once the agent has actually looked at the
        code -- opens the editing tools."""
        self.allowed = tuple(self.TOOLS)

    def _t_undo_last(self, args: dict) -> ToolResult:
        r = subprocess.run(["git", "-C", str(self.root), "reset", "--hard", "HEAD~1"],
                           capture_output=True, text=True, timeout=15)
        if r.returncode != 0:
            return ToolResult(False, "nothing to undo -- no earlier snapshot "
                              f"({(r.stderr or '').strip()[:120]})")
        return ToolResult(True, "reverted the last edit. " + (r.stdout or "").strip())

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
        if p.is_file():
            # the model passed a file where a dir goes -- list its parent
            # and point it at read_file rather than just erroring
            par = p.parent
            entries = sorted(c.name + ("/" if c.is_dir() else "") for c in par.iterdir())
            return ToolResult(True, f"'{rel}' is a file (use read_file for it). "
                                    f"contents of its directory:\n" + "\n".join(entries))
        if not p.is_dir():
            return ToolResult(False, f"no such directory: {rel} -- try list_dir with \".\"")
        entries = sorted(
            (c.name + ("/" if c.is_dir() else "")) for c in p.iterdir()
        )
        return ToolResult(True, "\n".join(entries) or "(empty)")
