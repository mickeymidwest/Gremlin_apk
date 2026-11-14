"""N10 Contradiction Hunter: a no-model, read-only sweep for drift between the
project's docs and the actual tree / config. It reports contradictions and
changes nothing -- the point is to catch the exact rot that keeps biting us:
a doc that points at a file that was renamed or deleted, or a model named in
the docs that isn't in config/models.yaml (or vice-versa).

Deterministic and dependency-light (yaml only, already a dep). Run it from a
weekly self-audit or by hand:  python -m magic_core.contradiction [root]
"""
from __future__ import annotations

import re
import sys
from dataclasses import dataclass
from pathlib import Path

_DOCS = ("MAGIC.md", "CALIBRATION.md", "GEMINI.md", "CLAUDE.md", "HARNESS-SURVEY.md",
         "README.md", ".magic/decisions.md", ".magic/proposals.md")

# a path that clearly points inside THIS repo (so we can check it exists)
_REPO_DIRS = ("magic_core/", "deploy/", "scripts/", "tests/", "bench/",
              "android/", "config/", "data/", ".magic/", ".github/")
_EXTS = ("py", "sh", "kt", "kts", "yaml", "yml", "service", "timer", "nft",
         "json", "jsonl", "md", "txt", "gradle", "xml", "cfg", "conf", "toml")
_PATH_RE = re.compile(
    r"`?((?:" + "|".join(re.escape(d) for d in _REPO_DIRS) +
    r")[A-Za-z0-9_./-]*\.(?:" + "|".join(_EXTS) + r"))\b`?")


@dataclass
class Contradiction:
    kind: str          # "dangling-file" | "model-doc-only"
    where: str         # the doc / file it was found in
    detail: str


def _read(p: Path) -> str:
    try:
        return p.read_text(errors="ignore")
    except OSError:
        return ""


def dangling_file_refs(root: Path) -> list[Contradiction]:
    """A repo file path written in a doc that does not exist in the tree."""
    out: list[Contradiction] = []
    seen: set[tuple[str, str]] = set()
    for doc in _DOCS:
        dp = root / doc
        if not dp.is_file():
            continue
        for m in _PATH_RE.finditer(_read(dp)):
            ref = m.group(1).rstrip(".,);:")
            if "*" in ref or "__" in ref or ref.endswith("/"):
                continue                      # a glob or a placeholder, not a real path
            if (root / ref).exists():
                continue
            key = (doc, ref)
            if key in seen:
                continue
            seen.add(key)
            out.append(Contradiction("dangling-file", doc,
                                     f"references {ref!r}, which is not in the tree"))
    return out


def _config_model_names(root: Path) -> set[str]:
    import yaml
    cfg = root / "config" / "models.yaml"
    if not cfg.is_file():
        return set()
    try:
        data = yaml.safe_load(_read(cfg)) or {}
    except Exception:
        return set()
    names = set()
    for m in (data.get("models") or []):
        if isinstance(m, dict) and m.get("name"):
            names.add(str(m["name"]))
    return names


def model_name_drift(root: Path, docs: "tuple[str, ...]" = (".magic/decisions.md",)) -> list[Contradiction]:
    """A model name a doc states is in use but that config/models.yaml does
    not define (a stale doc, or a model removed from config). Conservative:
    only flags names that LOOK like model ids (have a digit and a dash)."""
    cfg_names = _config_model_names(root)
    if not cfg_names:
        return []
    out: list[Contradiction] = []
    idlike = re.compile(r"`([a-z][a-z0-9.]*-[a-z0-9.-]*\d[a-z0-9.-]*)`")
    seen: set[str] = set()
    for doc in docs:
        dp = root / doc
        if not dp.is_file():
            continue
        for m in idlike.finditer(_read(dp)):
            name = m.group(1)
            if name in cfg_names or (doc, name) in seen:
                continue
            # only flag tokens that resemble one of the configured families
            # (match on the leading letters, so qwen2.5 ~ qwen3.5), so ordinary
            # back-ticked words are not false positives.
            def _fam(x):
                mm = re.match(r"[a-z]+", x)
                return mm.group(0) if mm else x
            if _fam(name) in {_fam(c) for c in cfg_names}:
                seen.add((doc, name))
                out.append(Contradiction("model-doc-only", doc,
                                         f"names model {name!r}, not in config/models.yaml"))
    return out


def scan(root: str) -> list[Contradiction]:
    r = Path(root)
    return dangling_file_refs(r) + model_name_drift(r)


def report(items: list[Contradiction]) -> str:
    if not items:
        return "No contradictions found between the docs and the tree."
    lines = [f"{len(items)} contradiction(s) between the docs and reality:"]
    for c in items:
        lines.append(f"  [{c.kind}] {c.where}: {c.detail}")
    return "\n".join(lines)


def main() -> int:
    root = sys.argv[1] if len(sys.argv) > 1 else "."
    items = scan(root)
    print(report(items))
    return 1 if items else 0


if __name__ == "__main__":
    raise SystemExit(main())
