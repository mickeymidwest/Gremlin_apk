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


def _decomment(s: str) -> str:
    """Drop string literals + // and /* */ comments so brace-counting on a
    line isn't thrown off by  char c = '}';  or  // end }"""
    s = re.sub(r'"(?:\\.|[^"\\\n])*"', '""', s)
    s = re.sub(r"'(?:\\.|[^'\\\n])'", "''", s)
    s = re.sub(r"/\*.*?\*/", "", s)
    s = re.sub(r"//[^\n]*", "", s)
    return s


def _block_end(lines: list[str], i: int) -> int:
    """Line index one past the end of the def/class block that starts at
    line i -- brace-balanced if it opens with '{', else by indentation."""
    if lines[i].rstrip().endswith("{"):
        depth = 0
        for k in range(i, len(lines)):
            c = _decomment(lines[k])
            depth += c.count("{") - c.count("}")
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
        if anchor is None and len(one) >= 12:    # strict fuzzy single line (no identifier swaps)
            want_ids = set(re.findall(r"[A-Za-z_]\w+", one))
            scored = sorted(
                ((difflib.SequenceMatcher(None, one, ln.strip()).ratio(), k)
                 for k, ln in enumerate(lines)),
                reverse=True)
            near = [
                k for r, k in scored
                if r >= 0.88 and want_ids.issubset(set(re.findall(r"[A-Za-z_]\w+", lines[k])))
            ]
            if len(near) == 1:
                anchor = (near[0], near[0] + 1)
            elif len(near) > 1:
                ns = ", ".join(str(k + 1) for k in sorted(near))
                return (f"your search closely matches {len(near)} lines ({ns}) -- ambiguous. "
                        "Give a multi-line search with a unique neighbouring line, or use write_file.")

    if anchor is None:
        # show the closest few lines so the model can correct
        best_r, best_k = 0.0, 0
        for k, ln in enumerate(lines):
            r = difflib.SequenceMatcher(None, norm(search), ln.strip()).ratio()
            if r > best_r:
                best_r, best_k = r, k
        lo, hi = max(0, best_k - 1), min(len(lines), best_k + 3)
        near = "".join(f"{idx + 1}: {lines[idx]}" for idx in range(lo, hi)).rstrip()
        return ("search text not found -- copy an exact snippet from read_file/view_file, "
                f"or use view_file(start={lo + 1}, count=40) to inspect lines {lo + 1}-{hi} first "
                f"(or write_file for a whole-file change).\nclosest lines:\n{near}")

    i, j = anchor
    # header-only search + multi-line replace -> replace the whole block
    if (j - i == 1 and "\n" in replace.strip() and _DECL_RE.search(lines[i])):
        j = _block_end(lines, i)
    return (i, j)


def _precheck(path: str, text: str) -> str:
    """Return a rejection message if `text` is obviously broken for its
    file type, else "". Cheap static checks only -- syntax, not logic."""
    try:
        from . import code_check as _cc
        _el = _cc.elision_in_source(text, path)
        if _el:
            return _el
    except Exception:
        pass
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
    elif path.endswith((".kt", ".kts")):
        # cheap Kotlin sanity: after stripping strings + comments, the
        # brackets must balance -- catches the 7B's #1 breakage (a dropped
        # } or )) before it reaches gradle
        s = re.sub(r'"""[\s\S]*?"""', '""', text)
        s = re.sub(r'"(?:\\.|[^"\\\n])*"', '""', s)
        s = re.sub(r"/\*.*?\*/", "", s, flags=re.DOTALL)
        s = re.sub(r"//[^\n]*", "", s)
        for op, cl, nm in (("{", "}", "braces"), ("(", ")", "parentheses")):
            if s.count(op) != s.count(cl):
                d = s.count(op) - s.count(cl)
                return (f"unbalanced {nm}: {abs(d)} more '{op if d > 0 else cl}' than "
                        f"'{cl if d > 0 else op}'. Count them in your replacement.")
    return ""


class ShellToolHost:
    TOOLS = {
        "repo_map":   "repo_map(query)                 -- map of the repo, ranked toward "
                      "`query`; read this before opening files",
        "grep":       "grep(pattern, path)             -- search files for a regex "
                      "(path defaults to '.'); use this to find where something is defined/used",
        "run_shell":  "run_shell(cmd)                  -- run a shell command in the repo root",
        "read_file":  "read_file(path)                 -- print a file's contents",
        "read_files": "read_files(paths)               -- batch-read up to 4 files in one step (list of paths)",
        "view_file":  "view_file(path, start, count)   -- print `count` lines of a file from line "
                      "`start`, with line numbers (for navigating a big file)",
        "find_symbol": "find_symbol(symbol, path)      -- locate exact line range & body of a function/class",
        "find_callers": "find_callers(symbol, path)    -- find definition + call/import references of `symbol` across files",
        "git_diff":   "git_diff(path)                  -- inspect your working tree diff before DONE",
        "mcp_read":   "mcp_read(tool, args)            -- run an allowed read-only MCP tool if configured",
        "web_docs":   "web_docs(query)                 -- search live online docs/APIs when stuck on an unfamiliar library, flag, or compile error",
        "edit_file":  "edit_file(path, search, replace) -- replace the first exact match of `search` "
                      "(prefer this over write_file for an existing file)",
        "multi_edit": "multi_edit(path, edits)         -- apply multiple non-overlapping {search, replace} hunks atomically",
        "write_file": "write_file(path, text)          -- overwrite a whole file with text",
        "undo_last":  "undo_last()                     -- revert your most recent edit "
                      "(rolls back the last per-step snapshot)",
        "revert_to_best": "revert_to_best()            -- reset working copy back to the highest-passing git snapshot",
        "list_dir":   "list_dir(path)                  -- list a directory (defaults to '.')",
        "read_ref":   "read_ref(ref, start, count)     -- read more of a large output that was stored as a ref (you will see 'stored as ref N'); pages `count` lines from line `start`",
        "skill_view": "skill_view(name)                -- load full steps of a skill card on demand",
        "delegate_task": "delegate_task(goal, file_slice, constraints) -- run an isolated read-only child worker returning {status, diff, short_summary}",
        "run_script_tools": "run_script_tools(calls)      -- run a declarative JSON batch (max 10) of [{\"name\": ..., \"args\": {...}}] allowed tool calls in one turn (no Python exec)",
    }

    # Phase-gated tool space (MAGIC.md section 8, #2): a small model does
    # much better when it can't edit before it has looked.
    EXPLORE_TOOLS = ("repo_map", "grep", "read_file", "read_files", "view_file", "find_symbol", "find_callers", "git_diff", "list_dir", "run_shell", "read_ref", "mcp_read", "web_docs", "skill_view", "delegate_task", "run_script_tools")

    # Tools that mutate state or chain/create tools -- always stripped in readonly mode.
    _READONLY_BLOCKED_TOOLS = frozenset({
        "write_file", "edit_file", "multi_edit", "undo_last",
        "revert_to_best", "forge_tool", "run_script_tools",
    })

    # Shell verbs that can change state -- blocked in read-only mode so
    # /do can answer "what's using my disk" by actually checking, without
    # any risk of it running something destructive.
    _WRITE_VERBS = (
        "rm", "mv", "cp", "dd", "mkfs", "shred", "truncate", ">", ">>", "tee",
        "chmod", "chown", "chattr", "ln", "install", "rsync", "kill", "pkill",
        "systemctl", "reboot", "shutdown", "poweroff", "mount", "umount",
        "pacman", "yay", "pip", "npm", "apt", "docker rm", "docker stop",
        "docker rmi", "docker kill", "git commit", "git push", "git reset",
        "git checkout", "git clean", "git apply", "git rm", "git mv", "git add",
        "git stash", "git rebase", "git merge", "git cherry-pick", "git config",
        "curl", "wget", "nc", "ncat",
        "touch", "mkdir", "rmdir", "mktemp", "patch", "crontab", "at",
        "sudo", "su", "pkexec", "doas", "env", "xargs", "eval", "exec", "source",
        "sh", "bash", "zsh", "dash", "fish", "ksh", "csh", "tcsh",
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
    #   awk ... system(...) / output redirection
    #   bash -c / sh -c / zsh -c          -- nested shell one-liner
    _READONLY_EXTRA = re.compile(
        r"""\$\(|`|<\(|>\("""
        r"""|['"a-zA-Z.]>>?|&>|>\|"""
        r"""|(?:sed|perl)\s(?:[^|;&]*\s)?-i\b"""
        r"""|-delete\b|-exec[a-z]*\s|-ok\s|-fprint\b"""
        r"""|\b(?:g|m|n)?awk\b[^|;&]*(?:\bsystem\s*\(|>>?|\|\s*['"])"""
        r"""|(?:^|[|;&\s])(?:\S*/)?(?:ba|z|da)?sh\b""")

    def __init__(self, root: str | Path, shell_timeout: int = 60, max_output: int = 8000,
                 allowed: "tuple[str, ...] | None" = None, readonly: bool = False,
                 protect_glob: str | None = None, forged=None, forged_root=None,
                 allow_web: bool = True, delegate_model=None):
        self.root = Path(root).resolve()
        self.shell_timeout = shell_timeout
        self.max_output = max_output
        self._allow_web = allow_web and os.environ.get("MAGIC_OFFLINE") != "1"
        self._delegate_model = delegate_model
        # Forged tools (data/forged_tools) joined into THIS instance's tool
        # space so a battle can call them as `forged_<name>`. In a readonly
        # battle only read_only forged tools are offered. The class TOOLS is
        # left untouched -- this is an instance-level override.
        self._forged_root = forged_root or str(Path(root).resolve())
        self._forged_names = set()
        extra = {}
        if forged:
            for f in forged:
                if readonly and f.get("capability") != "read_only":
                    continue
                nm = "forged_" + f.get("name", "")
                extra[nm] = (f"{nm}(args)  -- [forged] " + f.get("description", ""))[:160]
                self._forged_names.add(nm)
        # A building (non-readonly) battle may FORGE a new tool mid-run when it
        # lacks a capability. (A readonly battle writes nothing, so no forging.)
        if forged_root and not readonly:
            extra["forge_tool"] = (
                "forge_tool(name, description, code)  -- CREATE a new tool for yourself when you "
                "lack a capability. `code` must define run(args)->dict, standard library only, no "
                "destructive actions; it is security-checked + sandboxed, then usable immediately "
                "as forged_<name>.")
        if extra:
            self.TOOLS = {**self.TOOLS, **extra}
        self.allowed = tuple(allowed) if allowed is not None else tuple(self.TOOLS)
        self.readonly = readonly
        # paths (relative-glob) the agent may READ but not write -- e.g. the
        # target-under-test in a fuzzing battle, which must keep its bug
        self.protect_glob = protect_glob
        if readonly:
            self.allowed = tuple(t for t in self.allowed
                                 if t not in self._READONLY_BLOCKED_TOOLS)
        # Put the interpreter running Einherjar first on PATH so the agent's
        # `pytest` / `python` resolve to the env that actually has the test
        # deps -- otherwise a bare shell has neither and the agent can't
        # check its own work (seen on the first real run).
        # Strip secret env vars before running child commands cleanly (#768)
        _secret_env = re.compile(r"(?:^|_)(?:KEY|TOKEN|SECRET|PASSWORD|PASS|AUTH)$", re.I)
        self._env = {k: v for k, v in os.environ.items() if not _secret_env.search(k)}
        self._env["PATH"] = str(Path(sys.executable).parent) + os.pathsep + self._env.get("PATH", "")
        self._env["PYTHONDONTWRITEBYTECODE"] = "1"
        self._env["PIP_NO_INPUT"] = "1"
        self._env["GIT_TERMINAL_PROMPT"] = "0"
        # NOOA pass-by-reference: large outputs are stored here and shown as a
        # short preview + ref handle instead of dumped into context.
        self._refs: list = []
        self._ref_threshold = 1500

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
        """Pass-by-reference (NOOA): a small result is returned whole; a large
        one is STORED and replaced with a short preview + a ref handle, so a
        weak model is not flooded with a long dump. It reads more on demand
        with read_ref."""
        if len(text) <= self._ref_threshold:
            return text
        self._refs.append(text)
        rid = len(self._refs)
        preview = "\n".join(text.splitlines()[:15])
        if len(preview) > 900:
            preview = preview[:900]
        return (f"{preview}\n... [{len(text)} chars total -- stored as ref {rid}. "
                f"Use read_ref with {{\"ref\": {rid}}} (optionally \"start\", \"count\" "
                f"lines) to read more; the full output is NOT repeated here, to save "
                f"your context.]")

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
        if fn is None and call.name in getattr(self, "_forged_names", ()):
            if call.name not in self.allowed:
                return ToolResult(False, f"{call.name} isn't available yet. "
                                  f"Right now you can use: {', '.join(self.allowed)}. "
                                  "Look at the code first.")
            from .. import tool_forge
            _res = tool_forge.run_forged_sync(call.name[len("forged_"):], call.args,
                                              self._forged_root, timeout=self.shell_timeout)
            return ToolResult(bool(_res.get("ok")), str(_res.get("answer", "")))
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

    def _readonly_shell_block(self, cmd: str) -> str | None:
        """Return a block reason if `cmd` can mutate state in readonly mode, else None."""
        import shlex as _shlex
        low = cmd.lower()
        if self._READONLY_EXTRA.search(low):
            return "a redirect / substitution / shell / in-place edit"
        # Single-word command verbs checked by normalized basename (catches /bin/rm, /usr/bin/python3, env, xargs, bash)
        single_verbs = {v for v in self._WRITE_VERBS if " " not in v}
        two_word_verbs = {tuple(v.split()) for v in self._WRITE_VERBS if " " in v}
        # Split on shell separators outside quotes via shlex
        try:
            lex = _shlex.shlex(cmd, posix=True, punctuation_chars="|;&\n")
            lex.whitespace_split = True
            tokens = list(lex)
        except ValueError:
            return "unbalanced shell quoting"
        segments: list[list[str]] = [[]]
        for tok in tokens:
            if tok in ("|", "||", "&", "&&", ";", "\n"):
                if segments[-1]:
                    segments.append([])
            else:
                segments[-1].append(tok)
        for seg in segments:
            if not seg:
                continue
            # Skip leading VAR=val env assignments
            idx = 0
            while idx < len(seg) and re.match(r"^[A-Za-z_][A-Za-z0-9_]*=", seg[idx]):
                idx += 1
            if idx >= len(seg):
                continue
            cmd_tok = seg[idx]
            base = os.path.basename(cmd_tok).lower()
            if base in single_verbs or base.startswith("python"):
                return base
            if idx + 1 < len(seg):
                pair = (base, seg[idx + 1].lower())
                if pair in two_word_verbs:
                    return " ".join(pair)
                # git -C ... <subcmd>
                if base == "git":
                    for sub in seg[idx + 1:]:
                        if not sub.startswith("-"):
                            if ("git", sub.lower()) in two_word_verbs:
                                return f"git {sub.lower()}"
                            break
            # Check remaining tokens for redirection operators or sneaky interpreter/shell calls
            for t in seg[idx + 1:]:
                if t in (">", ">>", ">|", "&>", "&>>"):
                    return t
        return None

    def _t_run_shell(self, args: dict) -> ToolResult:
        cmd = self._val(args, "cmd", "command", "shell", "run")
        if not cmd:
            return ToolResult(False, "run_shell needs a 'cmd'")
        # models shorten "python -m pytest" to bare "pytest" (not on PATH
        # in the sandbox env) -- run it through the interpreter that has it
        cmd = re.sub(r"^(\s*)pytest(\s|$)", rf"\1{sys.executable} -m pytest\2", cmd)
        if self.readonly:
            hit = self._readonly_shell_block(cmd)
            if hit:
                return ToolResult(False, f"read-only mode: `{hit}` can change state and is blocked. "
                                         "Use a command that only reads (df, du, ls, ps, cat, ...).")
        run_cmd = cmd
        if re.search(r"\b(?:pytest|gradlew|cargo test|go test|npm test)\b", cmd):
            try:
                from .. import resource
                run_cmd = resource.cgroup_shell_cmd(cmd)
            except Exception:
                run_cmd = cmd
        proc = subprocess.run(
            run_cmd, shell=True, cwd=self.root, capture_output=True, text=True,
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

    def _t_read_files(self, args: dict) -> ToolResult:
        """Batch-read up to 4 paths (strings or {path, start, end} dicts), path-jailed."""
        raw = args.get("paths") or args.get("files") or args.get("path") or []
        if isinstance(raw, str):
            raw = [x.strip() for x in re.split(r"[,;\n]+", raw) if x.strip()]
        if not isinstance(raw, list) or not raw:
            return ToolResult(False, "read_files needs 'paths' as a non-empty list of repo paths")
        chunks: list[str] = []
        per_budget = max(500, 3200 // min(4, len(raw)))
        for item in raw[:4]:
            start_ln, end_ln = 0, 0
            if isinstance(item, dict):
                rel = str(item.get("path") or item.get("file") or "").strip()
                try:
                    start_ln = int(item.get("start") or item.get("start_line") or 0)
                    end_ln = int(item.get("end") or item.get("end_line") or 0)
                except (TypeError, ValueError):
                    start_ln, end_ln = 0, 0
            else:
                rel = str(item or "").strip()
            p = self._resolve(rel)
            if p is None:
                chunks.append(f"=== {rel}: path escapes repo root ===")
                continue
            if not p.is_file():
                chunks.append(f"=== {rel}: (not found) ===")
                continue
            try:
                txt = p.read_text(errors="replace")
            except OSError as e:
                chunks.append(f"=== {rel}: (read error: {e}) ===")
                continue
            hdr = rel
            if start_ln >= 1:
                all_lines = txt.splitlines()
                s_idx = max(1, start_ln)
                e_idx = min(len(all_lines), end_ln if end_ln >= s_idx else s_idx + 120)
                txt = "\n".join(all_lines[s_idx - 1:e_idx])
                hdr = f"{rel} [{s_idx}-{e_idx}/{len(all_lines)}]"
            if len(txt) > per_budget:
                txt = txt[:per_budget].rstrip() + "\n... (use view_file for later lines)"
            chunks.append(f"=== {hdr} ===\n{txt}")
        return ToolResult(True, self._clip("\n\n".join(chunks)))

    def _t_find_callers(self, args: dict) -> ToolResult:
        """Locate definition and call/import sites for a symbol across source files."""
        sym = self._val(args, "symbol", "name", "query", "pattern")
        if not sym:
            return ToolResult(False, "find_callers needs a 'symbol' name")
        rel = str(args.get("path") or args.get("dir") or ".").strip()
        target = self._resolve(rel)
        if target is None or not target.exists():
            return ToolResult(False, f"invalid path: {rel}")
        pat = re.compile(rf"\b{re.escape(sym)}\b")
        defs: list[str] = []
        uses: list[str] = []
        root_walk = [target] if target.is_file() else sorted(target.rglob("*"))
        for fp in root_walk:
            if not fp.is_file() or fp.suffix not in (".py", ".kt", ".kts", ".java", ".js", ".ts", ".tsx", ".go", ".rs"):
                continue
            rpath = fp.relative_to(self.root)
            if any(part in self._SKIP_MAP_DIRS for part in rpath.parts):
                continue
            try:
                lines = fp.read_text(errors="replace").splitlines()
            except OSError:
                continue
            for idx, ln in enumerate(lines, 1):
                if not pat.search(ln):
                    continue
                row = f"{rpath}:{idx}: {ln.strip()[:120]}"
                if _DECL_RE.search(ln) and sym in ln:
                    defs.append(row)
                else:
                    uses.append(row)
                if len(defs) + len(uses) >= 40:
                    break
            if len(defs) + len(uses) >= 40:
                break
        if not defs and not uses:
            return ToolResult(True, f"no references to {sym!r} found")
        out = []
        if defs:
            out.append("DEFINITIONS:\n" + "\n".join(defs[:10]))
        if uses:
            out.append("REFERENCES:\n" + "\n".join(uses[:25]))
        return ToolResult(True, self._clip("\n\n".join(out)))

    def _t_find_symbol(self, args: dict) -> ToolResult:
        """Locate exact [start-end] lines and source slice for a class/function."""
        sym = self._val(args, "symbol", "name", "query")
        if not sym:
            return ToolResult(False, "find_symbol needs a 'symbol' name")
        rel = str(args.get("path") or args.get("dir") or ".").strip()
        target = self._resolve(rel)
        if target is None or not target.exists():
            return ToolResult(False, f"invalid path: {rel}")
        files = [target] if target.is_file() else sorted(target.rglob("*.py"))
        hits: list[str] = []
        for fp in files:
            if not fp.is_file() or fp.suffix not in (".py", ".kt", ".java", ".js", ".ts", ".go", ".rs"):
                continue
            rpath = fp.relative_to(self.root)
            if any(part in self._SKIP_MAP_DIRS for part in rpath.parts):
                continue
            try:
                text = fp.read_text(errors="replace")
            except OSError:
                continue
            if fp.suffix == ".py":
                try:
                    tree = ast.parse(text)
                    lines = text.splitlines()
                    for node in ast.walk(tree):
                        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) and node.name == sym:
                            s_ln = node.lineno
                            e_ln = min(getattr(node, "end_lineno", None) or (s_ln + 35), s_ln + 60)
                            body = "\n".join(f"{ln}: {lines[ln - 1]}" for ln in range(s_ln, min(e_ln, len(lines)) + 1))
                            hits.append(f"=== {rpath} [{s_ln}-{e_ln}] ===\n{body}")
                except SyntaxError:
                    pass
            if not hits and sym in text:
                lines = text.splitlines()
                for idx, ln in enumerate(lines):
                    if _DECL_RE.search(ln) and re.search(rf"\b{re.escape(sym)}\b", ln):
                        e_ln = _block_end([l + "\n" for l in lines], idx)
                        body = "\n".join(f"{k + 1}: {lines[k]}" for k in range(idx, min(e_ln, idx + 45)))
                        hits.append(f"=== {rpath} [{idx + 1}-{e_ln}] ===\n{body}")
            if len(hits) >= 3:
                break
        if not hits:
            return ToolResult(True, f"(symbol {sym!r} not found under {rel})")
        return ToolResult(True, self._clip("\n\n".join(hits)))

    def _t_git_diff(self, args: dict) -> ToolResult:
        """Show working tree diff against HEAD cleanly inside the repo."""
        rel = str(args.get("path") or "").strip()
        cmd = ["git", "-C", str(self.root), "diff"]
        if rel:
            p = self._resolve(rel)
            if p is None:
                return ToolResult(False, "path escapes repo root")
            cmd += ["--", str(p.relative_to(self.root))]
        stat = subprocess.run(["git", "-C", str(self.root), "diff", "--stat"],
                              capture_output=True, text=True, timeout=10)
        res = subprocess.run(cmd, capture_output=True, text=True, timeout=15)
        out = ((stat.stdout or "").strip() + "\n\n" + (res.stdout or "").strip()).strip()
        return ToolResult(res.returncode == 0, self._clip(out or "(working tree clean)"))

    def _t_multi_edit(self, args: dict) -> ToolResult:
        """Atomically apply non-overlapping {search/old, replace/new} hunks on one file."""
        rel = self._val(args, "path", "file", "filename")
        p = self._resolve(rel)
        if p is None:
            return ToolResult(False, "path escapes the repo root")
        if self._protected(rel):
            return ToolResult(False, f"{rel} is READ-ONLY for this task -- do not change it.")
        if not p.is_file():
            return ToolResult(False, f"no such file: {rel}")
        edits = args.get("edits")
        if not isinstance(edits, list) or not edits:
            return ToolResult(False, "multi_edit needs non-empty 'edits' list of {search, replace}")
        orig = p.read_text()
        lines = orig.splitlines(keepends=True)
        spans: list[tuple[int, int, str]] = []
        for idx, item in enumerate(edits[:12]):
            if not isinstance(item, dict):
                return ToolResult(False, f"edits[{idx}] must be an object with search/replace")
            search = item.get("search", item.get("old", item.get("find", "")))
            replace = item.get("replace", item.get("new", item.get("with", "")))
            if not search:
                return ToolResult(False, f"edits[{idx}] missing non-empty 'search'")
            loc = _edit_locate(orig, str(search), str(replace))
            if isinstance(loc, str):
                return ToolResult(False, f"edits[{idx}]: {loc}")
            i, j = loc
            spans.append((i, j, str(replace)))
        spans.sort(key=lambda t: t[0])
        for k in range(len(spans) - 1):
            if spans[k][1] > spans[k + 1][0]:
                return ToolResult(False, f"overlapping hunks in multi_edit near line {spans[k + 1][0] + 1}")
        def _build_text(realign: bool) -> str:
            out_lines = list(lines)
            for i, j, rep in reversed(spans):
                rtxt = (_reindent(rep, _leading_ws(lines[i]), _leading_ws(rep))
                        if (realign and i < len(lines) and _leading_ws(lines[i]) and not _leading_ws(rep))
                        else rep)
                r_lines = (rtxt + "\n").splitlines(keepends=True) if rtxt and not rtxt.endswith("\n") else rtxt.splitlines(keepends=True)
                out_lines[i:j] = r_lines
            return "".join(out_lines)

        new_text = _build_text(realign=False)
        rej = _precheck(str(p), new_text)
        if rej:
            alt = _build_text(realign=True)
            if not _precheck(str(p), alt):
                new_text, rej = alt, ""
        if rej:
            return ToolResult(False, rej)
        p.write_text(new_text)
        return ToolResult(True, f"applied {len(spans)} edits to {rel}")

    def _t_revert_to_best(self, _args: dict) -> ToolResult:
        """Reset working tree back to 'magic-best' (highest observed pass count)
        or fall back to the battle baseline 'magic-start' snapshot."""
        target = None
        for tag in ("magic-best", "magic-start"):
            chk = subprocess.run(["git", "-C", str(self.root), "rev-parse", "--verify", tag],
                                 capture_output=True, text=True, timeout=10)
            if chk.returncode == 0:
                target = tag
                break
        if target is None:
            return ToolResult(False, "no 'magic-best' or 'magic-start' snapshot exists yet -- use undo_last instead")
        res = subprocess.run(["git", "-C", str(self.root), "reset", "--hard", target],
                             capture_output=True, text=True, timeout=15)
        if res.returncode != 0:
            return ToolResult(False, f"revert_to_best failed: {(res.stderr or '').strip()}")
        label = "highest-passing 'magic-best'" if target == "magic-best" else "baseline 'magic-start'"
        return ToolResult(True, f"reverted working tree cleanly to {label} snapshot")

    def _t_mcp_read(self, args: dict) -> ToolResult:
        """Run an allowed read-only MCP tool registered in tools.REGISTRY."""
        tname = self._val(args, "tool", "name")
        if not tname:
            return ToolResult(False, "mcp_read needs 'tool' (registered mcp_<server>_<tool> name)")
        import asyncio
        import concurrent.futures
        import inspect
        from .. import tools as tools_mod
        entry = tools_mod.REGISTRY.get(tname)
        if not entry or not tname.startswith("mcp_"):
            avail = sorted(t.name for t in tools_mod.REGISTRY.all() if t.name.startswith("mcp_"))
            return ToolResult(False, f"unknown MCP tool {tname!r}. Available: {avail or 'none'}")
        if getattr(entry, "capability", "") != "read_only":
            return ToolResult(False, f"{tname!r} is not marked read_only and is blocked inside battles")
        targs = args.get("args") if isinstance(args.get("args"), dict) else {}
        try:
            out = entry.handler(targs, {"root": str(self.root)})
            if inspect.isawaitable(out):
                try:
                    asyncio.get_running_loop()
                    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
                        out = pool.submit(asyncio.run, out).result(timeout=self.shell_timeout)
                except RuntimeError:
                    out = asyncio.run(out)
            if isinstance(out, dict):
                ok = bool(out.get("ok", True))
                txt = str(out.get("answer") if "answer" in out else out)
                return ToolResult(ok, self._clip(txt))
            return ToolResult(True, self._clip(str(out)))
        except Exception as e:
            return ToolResult(False, f"mcp_read {tname} failed: {e}")

    def _t_web_docs(self, args: dict) -> ToolResult:
        """Read-only online documentation/API lookup for battles (safe alternative
        to curl/wget which run_shell blocks)."""
        if not getattr(self, "_allow_web", True):
            return ToolResult(False, "web_docs is disabled in offline mode")
        query = self._val(args, "query", "q", "topic", "term")
        if not query:
            return ToolResult(False, "web_docs needs a non-empty 'query'")
        try:
            from .. import autoresearch as _ar, injection_guard as _ig, longctx as _lc, web_search as _ws
            results = _ws.search(query, limit=4)
            if not results:
                return ToolResult(False, f"no web docs found for {query!r}")
            ranked = _ar._rank_results(results, keep=3, query=query)
            lines = [f"Docs results for {query!r}:"]
            for i, r in enumerate(ranked, 1):
                lines.append(f"{i}. {(r.get('title') or '').strip()} -- {(r.get('url') or '').strip()}")
                snip = (r.get("snippet") or "").strip()
                if snip:
                    lines.append(f"   {snip}")
            top_url = (ranked[0].get("url") or "").strip()
            if top_url:
                raw = _ws.fetch_text(top_url, max_chars=8000)
                if raw and not raw.startswith(_ar._FETCH_FAIL):
                    lines.append(f"\nExtract ({top_url}):\n" + _lc.retrieve(raw, query, max_chars=1600).strip())
            out = "\n".join(lines)
            guard = _ig.guard_note(out, "web")
            if guard:
                out = guard + "\n" + out
            return ToolResult(True, self._clip(out))
        except Exception as e:
            return ToolResult(False, f"web_docs lookup failed: {e}")

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
        has_text = any(k in args for k in ("text", "content", "body"))
        text = args.get("text", args.get("content", args.get("body", "")))
        if not rel or not has_text or not str(text).strip():
            return ToolResult(False, "write_file needs non-empty 'path' and 'text' (pass the complete file contents in 'text')")
        # Parse-before-apply (MAGIC.md section 8, from SWE-agent's ACI): a
        # Python file that won't compile never lands -- the model gets the
        # SyntaxError back and fixes it instead of wasting the next few
        # turns discovering the break by running the tests.
        rej = _precheck(str(p), text)
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
        search = args.get("search", args.get("old", args.get("find", "")))
        replace = args.get("replace", args.get("new", args.get("with", "")))
        if not p.is_file():
            # a 7B often calls edit_file to CREATE a new file (empty search +
            # the whole body in replace). Treat that as write_file rather
            # than dead-ending it -- it's what it meant.
            if replace and not search:
                rej = _precheck(str(p), replace)
                if rej:
                    return ToolResult(False, f"NOT WRITTEN -- {rej}\nFix the syntax and try again.")
                p.parent.mkdir(parents=True, exist_ok=True)
                p.write_text(replace if replace.endswith("\n") else replace + "\n")
                return ToolResult(True, f"created {rel} ({len(replace)} chars)")
            return ToolResult(False, f"no such file: {rel or '(empty path)'} (use write_file to create it)")
        original = p.read_text()
        if not search:
            if replace:   # empty search + real replace = prepend header/import only
                if (original.strip() and "\n" in replace.strip()
                        and (_DECL_RE.search(replace) or "{" in replace or "LLVMFuzzerTestOneInput" in replace)):
                    return ToolResult(
                        False,
                        f"refused to prepend a whole function/block onto non-empty {rel} with empty 'search' -- "
                        "pass an exact 'search' snippet to replace, or use write_file(path, text) to rewrite the file.",
                    )
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

    def _t_read_ref(self, args: dict) -> "ToolResult":
        """Read a slice of a large output stored by pass-by-reference."""
        try:
            rid = int(self._val(args, "ref", "id", "n", "value") or 0)
        except (TypeError, ValueError):
            rid = 0
        if rid < 1 or rid > len(self._refs):
            return ToolResult(False, (f"no ref {rid}. Refs available: 1..{len(self._refs)}"
                              if self._refs else "no stored refs yet"))
        lines = self._refs[rid - 1].splitlines()
        try:
            start = max(1, int(args.get("start", 1) or 1))
            count = max(1, min(200, int(args.get("count", 80) or 80)))
        except (TypeError, ValueError):
            start, count = 1, 80
        chunk = lines[start - 1:start - 1 + count]
        more = ("" if start - 1 + count >= len(lines)
                else f"\n... [more -- read_ref ref={rid} start={start + count}]")
        return ToolResult(True, f"ref {rid}, lines {start}-{start + len(chunk) - 1} of "
                          f"{len(lines)}:\n" + "\n".join(chunk) + more)

    def _t_forge_tool(self, args: dict) -> "ToolResult":
        """Create a new tool mid-battle and make it usable right now. Runs the
        same forge as /forge (security scan + diagnostics + sandbox). The new
        tool is added to THIS toolhost's allowed set immediately."""
        from .. import tool_forge
        name = self._val(args, "name", "tool", "tool_name")
        desc = self._val(args, "description", "desc", "purpose")
        code = args.get("code") or args.get("source") or args.get("body") or ""
        if not name or not str(code).strip():
            return ToolResult(False, "forge_tool needs {name, description, code}; "
                              "code must define run(args) and return a dict")
        _req = tool_forge.approval_required(self._forged_root)
        r = tool_forge.forge(name, desc, str(code), root=self._forged_root,
                             capability="mutates_external", require_approval=_req)
        if not r.get("ok"):
            return ToolResult(False, "couldn't forge it: " + str(r.get("error", "")))
        if r.get("pending"):
            return ToolResult(True, f"forged {r['name']!r} -- it is PENDING the user's "
                              "approval, so it is NOT available in this battle. Carry on without it.")
        nm = "forged_" + r["name"]
        self.TOOLS = {**self.TOOLS, nm: (f"{nm}(args)  -- [forged] " + (desc or ""))[:160]}
        self._forged_names.add(nm)
        if nm not in self.allowed:
            self.allowed = tuple(self.allowed) + (nm,)
        return ToolResult(True, f"forged {nm} -- you can call it now as  ACTION: {nm}")

    def unlock_all(self) -> None:
        """Called by battle.py once the agent has actually looked at the
        code -- opens the editing tools (unless readonly=True)."""
        if self.readonly:
            self.allowed = tuple(t for t in self.TOOLS if t not in self._READONLY_BLOCKED_TOOLS)
        else:
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

    # Regex symbol extractors for non-Python source (HARNESS-SURVEY #11).
    # Conservative line-anchored patterns -- a map, not a compiler: better to
    # miss an oddly-formatted decl than to print garbage. The capturing group
    # that holds the name differs per pattern (see each finditer below).
    _SYM_KTJAVA = re.compile(
        r"""(?m)^[ \t]*(?:@[\w.]+[ \t]*)*"""
        r"""(?:(?:public|private|protected|internal|open|abstract|sealed|final|"""
        r"""data|companion|override|suspend|inline|external|static)[ \t]+)*"""
        r"""(class|object|interface|enum[ \t]+class|enum)[ \t]+([A-Za-z_]\w*)""")
    _SYM_KT_FUN = re.compile(
        r"""(?m)^[ \t]*(?:@[\w.]+[ \t]*)*"""
        r"""(?:(?:public|private|protected|internal|open|override|suspend|inline"""
        r"""|operator|tailrec|external)[ \t]+)*"""
        r"""fun[ \t]+(?:<[^>\n]+>[ \t]*)?(?:[\w.]+\.)?([A-Za-z_]\w*)[ \t]*\(""")
    _SYM_JAVA_METH = re.compile(
        r"""(?m)^[ \t]*(?:@[\w.]+[ \t]*)*"""
        r"""(?:(?:public|private|protected|static|final|synchronized|abstract)[ \t]+)+"""
        r"""[\w<>\[\],. ]+?[ \t]+([A-Za-z_]\w*)[ \t]*\([^;{)]*\)[ \t]*(?:throws[^{;]+)?\{""")
    _SYM_JS = re.compile(
        r"""(?m)^[ \t]*(?:export[ \t]+)?(?:default[ \t]+)?"""
        r"""(?:async[ \t]+)?(?:function\*?[ \t]+([A-Za-z_$][\w$]*)"""
        r"""|class[ \t]+([A-Za-z_$][\w$]*)"""
        r"""|(?:const|let|var)[ \t]+([A-Za-z_$][\w$]*)[ \t]*=[ \t]*(?:async[ \t]+)?(?:\([^)]*\)|[\w$]+)[ \t]*=>)""")
    _SYM_GO = re.compile(
        r"""(?m)^[ \t]*(?:func[ \t]+(?:\([^)]*\)[ \t]*)?([A-Za-z_]\w*)[ \t]*\("""
        r"""|type[ \t]+([A-Za-z_]\w*)[ \t]+(?:struct|interface)\b)""")
    _SYM_RUST = re.compile(
        r"""(?m)^[ \t]*(?:pub[ \t]+)?(?:async[ \t]+)?"""
        r"""(?:fn[ \t]+([A-Za-z_]\w*)|struct[ \t]+([A-Za-z_]\w*)"""
        r"""|enum[ \t]+([A-Za-z_]\w*)|trait[ \t]+([A-Za-z_]\w*))""")

    @classmethod
    def _source_symbols(cls, text: str, suffix: str) -> list[str]:
        """Up to ~24 top declarations from a source file, in file order, as
        short label strings like 'class Foo' / 'fun bar('. Empty if the
        language isn't one we extract."""
        out: list[str] = []
        seen: set[str] = set()

        def add(label: str):
            if label not in seen:
                seen.add(label)
                out.append(label)

        if suffix in (".kt", ".kts", ".java"):
            pos = {}
            for m in cls._SYM_KTJAVA.finditer(text):
                kind = re.sub(r"\s+", " ", m.group(1))
                pos.setdefault(m.start(), f"{kind} {m.group(2)}")
            for m in cls._SYM_KT_FUN.finditer(text):
                pos.setdefault(m.start(), f"fun {m.group(1)}(")
            if suffix == ".java":
                for m in cls._SYM_JAVA_METH.finditer(text):
                    pos.setdefault(m.start(), f"{m.group(1)}(")
            for _, label in sorted(pos.items()):
                add(label)
        elif suffix in (".js", ".ts", ".tsx", ".jsx", ".mjs"):
            for m in cls._SYM_JS.finditer(text):
                name = m.group(1) or m.group(2) or m.group(3)
                if name:
                    add(("class " if m.group(2) else "function ") + name)
        elif suffix == ".go":
            for m in cls._SYM_GO.finditer(text):
                if m.group(1):
                    add(f"func {m.group(1)}(")
                elif m.group(2):
                    add(f"type {m.group(2)}")
        elif suffix in (".rs",):
            for m in cls._SYM_RUST.finditer(text):
                for kind, g in (("fn", 1), ("struct", 2), ("enum", 3), ("trait", 4)):
                    if m.group(g):
                        add(f"{kind} {m.group(g)}" + ("(" if kind == "fn" else ""))
        return out[:24]

    _SRC_EXT = (".kt", ".kts", ".java", ".js", ".ts", ".tsx", ".jsx", ".mjs", ".go", ".rs",
                ".c", ".h", ".cc", ".cpp", ".hpp", ".swift", ".rb", ".sh", ".gradle")

    def _nonpython_map(self, query: str) -> ToolResult:
        qwords = set(re.findall(r"[a-z_]{3,}", query.lower()))
        blocks: list[tuple[int, str]] = []   # (score, text)
        files: list[tuple[str, int]] = []
        for p in sorted(self.root.rglob("*")):
            rel = p.relative_to(self.root)
            if any(part in self._SKIP_MAP_DIRS for part in rel.parts):
                continue
            if not (p.is_file() and (p.suffix in self._SRC_EXT or p.name in (
                    "settings.gradle.kts", "build.gradle.kts", "AndroidManifest.xml",
                    "package.json", "Cargo.toml", "go.mod"))):
                continue
            try:
                raw = p.read_bytes()
            except OSError:
                continue
            n = len(raw.splitlines())
            files.append((str(rel), n))
            syms = self._source_symbols(
                raw[:200_000].decode("utf-8", "replace"), p.suffix)
            if not syms:
                continue
            text = "\n".join([str(rel)] + [f"    {x}" for x in syms])
            hay = set(re.findall(r"[a-z_]{3,}", text.lower()))
            score = len(qwords & hay) if qwords else 0
            blocks.append((score, text))
        out: list[str] = []
        if blocks:
            blocks.sort(key=lambda b: -b[0])
            out.append("symbols (ranked toward your query):")
            out += [t for _, t in blocks[:40]]
            out.append("")
        out.append("source files:")
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

    def _t_skill_view(self, args: dict) -> ToolResult:
        """Progressive disclosure: load full procedure for a skill card on demand."""
        name = self._val(args, "name", "skill", "id", "query")
        if not name:
            return ToolResult(False, "skill_view needs a 'name'")
        try:
            from .store import Store
            skills = Store(str(self.root)).read_skills()
        except Exception as e:
            return ToolResult(False, f"couldn't load skills: {e}")
        low = name.strip().lower()
        exact = [s for s in skills if s.name.lower() == low or s.id.lower() == low]
        if not exact:
            exact = [s for s in skills if low in s.name.lower() or low in s.purpose.lower()]
        if not exact:
            avail = ", ".join(s.name for s in skills[:15])
            return ToolResult(False, f"no skill matching {name!r}. Examples: {avail}")
        return ToolResult(True, self._clip("\n\n".join(s.render() for s in exact[:3])))

    def _t_delegate_task(self, args: dict) -> ToolResult:
        """Run an isolated read-only child worker with structured handoff."""
        goal = self._val(args, "goal", "task", "prompt", "question")
        if not goal:
            return ToolResult(False, "delegate_task needs a 'goal'")
        if self._delegate_model is None:
            return ToolResult(False, "delegate_task has no model configured in this session")
        file_slice = str(args.get("file_slice") or args.get("files") or "").strip()
        constraints = str(args.get("constraints") or "").strip()
        from . import subagent
        import json as _json
        res = subagent.delegate_structured(
            goal, self._delegate_model, str(self.root),
            file_slice=file_slice, constraints=constraints,
        )
        return ToolResult(res.get("status") == "ok", self._clip(_json.dumps(res)))

    def _t_run_script_tools(self, args: dict) -> ToolResult:
        """Single-turn declarative batch of allowed tool calls (NO in-process Python exec).
        Accepts `calls`: a list (or JSON string) of {"name": str, "args": dict} objects (max 10)."""
        import json as _json
        raw_calls = args.get("calls")
        if raw_calls is None:
            raw_calls = args.get("script") if "script" in args else args.get("code")
        if raw_calls is None:
            return ToolResult(False, "run_script_tools needs 'calls' as a JSON list of {name, args} objects")
        if isinstance(raw_calls, str):
            s = raw_calls.strip()
            if not s:
                return ToolResult(False, "run_script_tools needs non-empty 'calls'")
            if len(s) > 6000:
                return ToolResult(False, "calls payload too long (>6000 chars)")
            try:
                parsed = _json.loads(s)
            except ValueError:
                return ToolResult(
                    False,
                    "run_script_tools only accepts a declarative JSON list of {name, args} calls; "
                    "arbitrary Python execution is disabled",
                )
            if isinstance(parsed, dict) and isinstance(parsed.get("calls"), list):
                calls = parsed["calls"]
            elif isinstance(parsed, list):
                calls = parsed
            else:
                return ToolResult(False, "run_script_tools JSON must be a list of {name, args} objects")
        elif isinstance(raw_calls, list):
            calls = raw_calls
        else:
            return ToolResult(False, "run_script_tools 'calls' must be a list of {name, args} objects")

        if not calls:
            return ToolResult(False, "run_script_tools 'calls' list is empty")
        if len(calls) > 10:
            return ToolResult(False, "run_script_tools budget exceeded (max 10 calls)")

        results: list[dict] = []
        for idx, item in enumerate(calls):
            if not isinstance(item, dict):
                return ToolResult(False, f"calls[{idx}] must be an object with 'name' and optional 'args'")
            tname = str(item.get("name") or item.get("tool") or "").strip()
            targs = item.get("args") if isinstance(item.get("args"), dict) else {}
            if not tname:
                return ToolResult(False, f"calls[{idx}] missing tool 'name'")
            if tname in ("run_script_tools", "delegate_task"):
                return ToolResult(False, f"recursive/nested tool {tname!r} is disallowed inside run_script_tools")
            res = self.run(ToolCall(name=tname, args=targs))
            results.append({"name": tname, "ok": res.ok, "output": res.output})
        all_ok = all(r["ok"] for r in results)
        return ToolResult(all_ok, self._clip(_json.dumps(results)))
