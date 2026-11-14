"""Smart context bundling (Magic's request, turns 94/95): when a task centres
on a file, pull in the files it actually DEPENDS on and the files that depend on
IT, so the model sees the real blast radius of a change instead of a hand-typed
file list -- "smarter selection, not a bigger window".

Deterministic, no model. Python import graph (the codebase), one hop each way,
with a same-directory-sibling fallback for other languages.

`magic context <file> [...]`
"""
from __future__ import annotations

import ast
import re
from pathlib import Path

_SKIP = {".git", "venv", ".venv", "__pycache__", "node_modules", "build", "dist"}


def _rel(repo: Path, p: Path) -> str:
    try:
        return str(p.resolve().relative_to(repo.resolve()))
    except ValueError:
        return str(p)


def _mod_to_path(repo: Path, mod: str) -> "str | None":
    """magic_core.magic.foo -> magic_core/magic/foo.py (or .../foo/__init__.py)."""
    base = mod.replace(".", "/")
    for cand in (repo / f"{base}.py", repo / base / "__init__.py"):
        if cand.is_file():
            return _rel(repo, cand)
    return None


def imports_of(repo: str, relpath: str) -> "list[str]":
    """Local repo files that `relpath` imports (Python)."""
    r = Path(repo)
    f = r / relpath
    if f.suffix != ".py":
        return []
    try:
        tree = ast.parse(f.read_text())
    except (OSError, SyntaxError):
        return []
    pkg_parts = Path(relpath).parent.parts            # for resolving relative imports
    out: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for n in node.names:
                p = _mod_to_path(r, n.name)
                if p:
                    out.append(p)
        elif isinstance(node, ast.ImportFrom):
            if node.level:                            # relative: from . / .. import x
                base = list(pkg_parts[: len(pkg_parts) - (node.level - 1)]) if node.level > 1 else list(pkg_parts)
                mod = "/".join(base + ([node.module.replace(".", "/")] if node.module else []))
                for n in node.names:
                    for cand in (r / f"{mod}/{n.name}.py", r / mod / n.name / "__init__.py", r / f"{mod}.py"):
                        if cand.is_file():
                            out.append(_rel(r, cand))
                            break
            elif node.module:
                p = _mod_to_path(r, node.module)
                if p:
                    out.append(p)
                for n in node.names:                  # from pkg import submodule
                    sp = _mod_to_path(r, f"{node.module}.{n.name}")
                    if sp:
                        out.append(sp)
    seen: list[str] = []
    for x in out:
        if x not in seen and x != relpath:
            seen.append(x)
    return seen


def importers_of(repo: str, relpath: str) -> "list[str]":
    """Local .py files that import the module at `relpath`."""
    r = Path(repo)
    rel = relpath[:-3] if relpath.endswith(".py") else relpath
    mod_dotted = rel.replace("/", ".").removesuffix(".__init__")
    leaf = Path(rel).name
    pat = re.compile(rf"(?:^|\b)(?:import\s+{re.escape(mod_dotted)}\b|"
                     rf"from\s+[\w.]*\b{re.escape(leaf)}\s+import\b|"
                     rf"from\s+{re.escape(mod_dotted)}\s+import\b)", re.M)
    out: list[str] = []
    for p in r.rglob("*.py"):
        if any(part in _SKIP for part in p.relative_to(r).parts):
            continue
        rp = _rel(r, p)
        if rp == relpath:
            continue
        try:
            if pat.search(p.read_text()):
                out.append(rp)
        except OSError:
            continue
    return sorted(out)


def bundle(repo: str, seeds, *, max_files: int = 12) -> "list[str]":
    """seeds + their imports + their importers, de-duplicated, seeds first,
    capped at max_files. For a non-Python seed, falls back to same-dir siblings."""
    r = Path(repo)
    ordered: list[str] = []

    def add(x: str):
        if x and x not in ordered and (r / x).is_file():
            ordered.append(x)

    seeds = [seeds] if isinstance(seeds, str) else list(seeds)
    for s in seeds:
        add(s)
    for s in seeds:
        if s.endswith(".py"):
            for dep in imports_of(repo, s):
                add(dep)
            for imp in importers_of(repo, s):
                add(imp)
        else:                                          # non-Python: same-dir siblings
            d = (r / s).parent
            for sib in sorted(d.glob(f"*{(r / s).suffix}")) if d.is_dir() else []:
                add(_rel(r, sib))
    return ordered[:max_files]


def report(repo: str, seeds, **kw) -> str:
    files = bundle(repo, seeds, **kw)
    if not files:
        return "No context bundle (seed file not found)."
    return "Context bundle (seed + its deps + dependents):\n" + "\n".join(f"  {f}" for f in files)
