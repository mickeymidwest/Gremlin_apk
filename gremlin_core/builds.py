"""
Retrieve projects Gremlin has built (via build_project -> ~/Downloads/<name>/)
so they can be pulled down through the phone app.

A build folder is identified by a `.gremlin-build.json` marker that
build_project drops on a successful build -- that's what keeps this from
ever offering up ~/Downloads/gremlin itself, the robofuse stack, or any
other unrelated folder that happens to live there.

The server exposes:
    GET /builds            -> list_builds()  (metadata, no file contents)
    GET /builds/<name>     -> a .zip of that folder  (see make_zip)
"""
from __future__ import annotations

import io
import json
import re
import time
import zipfile
from pathlib import Path
from typing import Optional

BUILDS_DIR = Path.home() / "Downloads"
MARKER = ".gremlin-build.json"

# Never zipped up and sent to the phone -- big, machine-specific, or noise.
_SKIP_DIRS = {".git", "venv", ".venv", "node_modules", "__pycache__",
              ".pytest_cache", ".mypy_cache", "target", "dist", "build"}
_NAME_RE = re.compile(r"^[A-Za-z0-9_-]+$")

# A single build's zip is capped so a runaway/huge folder can't wedge the
# phone download or the server's memory (the zip is built in RAM).
MAX_ZIP_BYTES = 50 * 1024 * 1024


def write_marker(target_root: str, goal: str, models: list[str], files_changed: list[str]) -> None:
    """Called by build_project after a successful, committed build."""
    path = Path(target_root) / MARKER
    payload = {
        "goal": goal,
        "created": time.time(),
        "models": models,
        "files_changed": files_changed or [],
    }
    try:
        path.write_text(json.dumps(payload, indent=2))
    except OSError:
        pass  # a missing marker just means this build won't be listed; not fatal


def _dir_stats(root: Path) -> tuple[int, int]:
    """(total_bytes, file_count) for what make_zip would actually include."""
    total = count = 0
    for p in root.rglob("*"):
        if any(part in _SKIP_DIRS for part in p.relative_to(root).parts):
            continue
        if p.is_file():
            try:
                total += p.stat().st_size
                count += 1
            except OSError:
                pass
    return total, count


def _resolve(name: str) -> Optional[Path]:
    """A validated build folder for `name`, or None. Rejects anything
    that isn't a bare name, isn't directly under ~/Downloads, or has no
    marker -- so this can never be walked out to an arbitrary path."""
    if not _NAME_RE.match(name or ""):
        return None
    root = (BUILDS_DIR / name).resolve()
    if root.parent != BUILDS_DIR.resolve() or not root.is_dir():
        return None
    if not (root / MARKER).is_file():
        return None
    return root


def list_builds() -> list[dict]:
    """Every build folder under ~/Downloads, newest first."""
    out = []
    if not BUILDS_DIR.is_dir():
        return out
    for child in BUILDS_DIR.iterdir():
        if not child.is_dir():
            continue
        marker = child / MARKER
        if not marker.is_file():
            continue
        try:
            meta = json.loads(marker.read_text())
        except (OSError, ValueError):
            meta = {}
        size, files = _dir_stats(child)
        out.append({
            "name": child.name,
            "goal": meta.get("goal", ""),
            "created": meta.get("created"),
            "models": meta.get("models", []),
            "size_bytes": size,
            "file_count": files,
            "too_big": size > MAX_ZIP_BYTES,
        })
    out.sort(key=lambda b: b.get("created") or 0, reverse=True)
    return out


def make_zip(name: str) -> Optional[tuple[bytes, str]]:
    """(zip_bytes, download_filename) for a build, or None if `name`
    doesn't resolve to a real build folder. Raises ValueError if the
    folder is over MAX_ZIP_BYTES."""
    root = _resolve(name)
    if root is None:
        return None

    size, _ = _dir_stats(root)
    if size > MAX_ZIP_BYTES:
        raise ValueError(f"{name} is {size // 1024 // 1024} MB, over the {MAX_ZIP_BYTES // 1024 // 1024} MB limit")

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for p in sorted(root.rglob("*")):
            rel = p.relative_to(root)
            if any(part in _SKIP_DIRS for part in rel.parts):
                continue
            if p.is_file():
                zf.write(p, arcname=str(Path(name) / rel))
    return buf.getvalue(), f"{name}.zip"
