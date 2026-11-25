"""Safe multipart attachment storage and dynamic-context preparation.

Uploaded files live under gitignored ``data/magic/uploads/`` (with metadata
sidecars in ``data/magic/uploads/.meta/``). Text/code excerpts, APK summaries,
and binary/image metadata are injected strictly into dynamic prompt context so
the 1,807-token R5 static prefix cache stays invariant.
"""
from __future__ import annotations

import contextvars
import json
import mimetypes
import os
import re
import time
import uuid
import zipfile
from pathlib import Path
from typing import Any, BinaryIO, Iterable, Optional

DEFAULT_ROOT = Path(__file__).resolve().parent.parent

MAX_APK_BYTES = 50 * 1024 * 1024
MAX_OTHER_BYTES = 10 * 1024 * 1024
MAX_TEXT_EXCERPT_CHARS = 8000
_CHUNK_BYTES = 65536

_TEXT_EXTENSIONS = {
    "txt", "md", "markdown", "csv", "tsv", "json", "xml", "yaml", "yml",
    "log", "ini", "cfg", "conf", "properties", "sh", "bash", "zsh",
    "py", "kt", "kts", "java", "js", "mjs", "cjs", "ts", "tsx", "jsx",
    "c", "h", "cpp", "hpp", "rs", "go", "rb", "php", "sql", "html", "htm",
    "css", "toml", "gradle", "env", "diff", "patch",
}

_IMAGE_EXTENSIONS = {"png", "jpg", "jpeg", "gif", "webp", "bmp", "heic"}

_TEXT_MIME_PREFIXES = ("text/",)
_TEXT_MIMES = {
    "application/json",
    "application/xml",
    "application/x-yaml",
    "application/yaml",
    "application/javascript",
    "application/x-sh",
    "application/toml",
}

_SAFE_NAME_RE = re.compile(r"[^A-Za-z0-9._-]+")
_ATTACH_ID_RE = re.compile(r"^[A-Za-z0-9_-]{8,64}$")

_TURN_ATTACHMENT_CTX: contextvars.ContextVar[str] = contextvars.ContextVar(
    "magic_turn_attachment_ctx", default=""
)


class UploadTooLargeError(ValueError):
    """Raised when an uploaded file exceeds its per-type byte limit."""


def current_attachment_context() -> str:
    return _TURN_ATTACHMENT_CTX.get() or ""


def set_turn_attachment_context(text: str) -> contextvars.Token:
    return _TURN_ATTACHMENT_CTX.set(text or "")


def reset_turn_attachment_context(tok: contextvars.Token) -> None:
    _TURN_ATTACHMENT_CTX.reset(tok)


def uploads_dir(project_root: Optional[str | Path] = None) -> Path:
    base = Path(project_root) if project_root else DEFAULT_ROOT
    return (base / "data" / "magic" / "uploads").resolve()


def sanitize_filename(raw_name: Optional[str]) -> str:
    """Strip any directory components or traversal sequences from `raw_name`."""
    s = str(raw_name or "").replace("\\", "/")
    base = s.split("/")[-1].strip()
    # Strip any '..' segments and non-safe characters.
    base = base.replace("..", ".")
    cleaned = _SAFE_NAME_RE.sub("_", base).strip("._")
    if not cleaned:
        return "upload.bin"
    if len(cleaned) > 120:
        stem, dot, ext = cleaned.rpartition(".")
        if dot and len(ext) <= 16:
            cleaned = f"{stem[:120 - len(ext) - 1]}.{ext}"
        else:
            cleaned = cleaned[:120]
    return cleaned


def _format_size(n: int) -> str:
    if n < 1024:
        return f"{n} B"
    if n < 1024 * 1024:
        return f"{n / 1024:.1f} KB"
    return f"{n / (1024 * 1024):.2f} MB"


def _is_apk_file(path: Path, filename: str, mime_type: str) -> bool:
    if filename.lower().endswith(".apk") or mime_type == "application/vnd.android.package-archive":
        return True
    try:
        with open(path, "rb") as f:
            head = f.read(4)
        if head.startswith(b"PK\x03\x04"):
            with zipfile.ZipFile(path, "r") as zf:
                names = set(zf.namelist())
                if "AndroidManifest.xml" in names or "classes.dex" in names:
                    return True
    except Exception:
        pass
    return False


def _image_dimensions(path: Path) -> Optional[tuple[int, int]]:
    try:
        from PIL import Image  # type: ignore

        with Image.open(path) as img:
            w, h = img.size
            return int(w), int(h)
    except Exception:
        return None


def _classify_file(path: Path, filename: str, raw_mime: str) -> tuple[str, str, str, Optional[tuple[int, int]]]:
    """Return ``(kind, mime_type, suggested_action, dimensions)``."""
    ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
    guessed_mime, _ = mimetypes.guess_type(filename)
    mime = (raw_mime or guessed_mime or "application/octet-stream").split(";")[0].strip().lower()

    if _is_apk_file(path, filename, mime):
        return "apk", "application/vnd.android.package-archive", "inspect", None

    if mime.startswith("image/") or ext in _IMAGE_EXTENSIONS:
        dims = _image_dimensions(path)
        eff_mime = mime if mime.startswith("image/") else (guessed_mime or f"image/{ext or 'png'}")
        return "image", eff_mime, "chat", dims

    if mime.startswith(_TEXT_MIME_PREFIXES) or mime in _TEXT_MIMES or ext in _TEXT_EXTENSIONS:
        eff_mime = mime if (mime.startswith("text/") or mime in _TEXT_MIMES) else "text/plain"
        return "text", eff_mime, "chat", None

    # Sniff first 2 KB for plain UTF-8 text when extension/MIME was generic.
    try:
        sample = path.read_bytes()[:2048]
        if sample and b"\x00" not in sample:
            sample.decode("utf-8")
            return "text", "text/plain", "chat", None
    except Exception:
        pass

    return "binary", mime or "application/octet-stream", "chat", None


def save_upload(
    stream: BinaryIO,
    raw_filename: Optional[str],
    *,
    content_type: str = "",
    project_root: Optional[str | Path] = None,
    content_length: Optional[int] = None,
) -> dict[str, Any]:
    """Stream an uploaded file safely to ``data/magic/uploads/`` under size limits."""
    root_dir = uploads_dir(project_root)
    meta_dir = root_dir / ".meta"
    root_dir.mkdir(parents=True, exist_ok=True)
    meta_dir.mkdir(parents=True, exist_ok=True)

    filename = sanitize_filename(raw_filename)
    ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
    mime_clean = (content_type or "").split(";")[0].strip().lower()
    is_apk_hint = ext == "apk" or mime_clean == "application/vnd.android.package-archive"
    max_bytes = MAX_APK_BYTES if is_apk_hint else MAX_OTHER_BYTES

    if content_length is not None and content_length > max_bytes:
        raise UploadTooLargeError(
            f"File {filename!r} exceeds limit ({_format_size(max_bytes)} max)"
        )

    aid = uuid.uuid4().hex[:16]
    dest = (root_dir / f"{aid}_{filename}").resolve()
    if dest.parent != root_dir:
        raise ValueError("Invalid destination path")
    tmp_path = (root_dir / f".tmp_{aid}_{uuid.uuid4().hex[:8]}").resolve()

    total = 0
    try:
        with open(tmp_path, "wb") as out:
            while True:
                chunk = stream.read(_CHUNK_BYTES)
                if not chunk:
                    break
                total += len(chunk)
                if total > max_bytes:
                    raise UploadTooLargeError(
                        f"File {filename!r} exceeds limit ({_format_size(max_bytes)} max)"
                    )
                out.write(chunk)
        os.replace(tmp_path, dest)
    except Exception:
        try:
            tmp_path.unlink(missing_ok=True)
        except OSError:
            pass
        try:
            dest.unlink(missing_ok=True)
        except OSError:
            pass
        raise

    kind, mime_type, suggested_action, dims = _classify_file(dest, filename, mime_clean)
    meta: dict[str, Any] = {
        "attachment_id": aid,
        "filename": filename,
        "stored_name": dest.name,
        "mime_type": mime_type,
        "size_bytes": int(total),
        "kind": kind,
        "suggested_action": suggested_action,
        "created_at": time.time(),
    }
    if dims is not None:
        meta["width"] = dims[0]
        meta["height"] = dims[1]

    meta_path = (meta_dir / f"{aid}.json").resolve()
    meta_path.write_text(json.dumps(meta, indent=2), encoding="utf-8")

    return {"ok": True, **meta}


def get_attachment(
    attachment_id: str,
    *,
    project_root: Optional[str | Path] = None,
) -> Optional[dict[str, Any]]:
    aid = str(attachment_id or "").strip()
    if not _ATTACH_ID_RE.match(aid):
        return None
    root_dir = uploads_dir(project_root)
    meta_path = (root_dir / ".meta" / f"{aid}.json").resolve()
    if meta_path.parent != (root_dir / ".meta").resolve() or not meta_path.is_file():
        return None
    try:
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(meta, dict):
        return None
    stored_name = sanitize_filename(meta.get("stored_name") or "")
    file_path = (root_dir / stored_name).resolve()
    if file_path.parent != root_dir or not file_path.is_file():
        return None
    return {**meta, "path": str(file_path)}


def delete_attachment(attachment_id: str, *, project_root: Optional[str | Path] = None) -> bool:
    aid = str(attachment_id or "").strip()
    if not _ATTACH_ID_RE.match(aid):
        return False
    root_dir = uploads_dir(project_root)
    meta_path = (root_dir / ".meta" / f"{aid}.json").resolve()
    removed = False
    meta = get_attachment(aid, project_root=project_root)
    if meta and meta.get("path"):
        try:
            Path(meta["path"]).unlink(missing_ok=True)
            removed = True
        except OSError:
            pass
    if meta_path.is_file():
        try:
            meta_path.unlink(missing_ok=True)
            removed = True
        except OSError:
            pass
    return removed


def should_direct_inspect_apk(message: str) -> bool:
    """True when an APK attachment has no specific non-inspect question."""
    low = (message or "").strip().lower()
    if not low or low.startswith("[attached"):
        return True
    if low in ("inspect", "inspect this", "inspect apk", "inspect this apk", "analyze", "analyze apk", "scan"):
        return True
    return bool(re.match(r"^(?:please\s+)?(?:inspect|analyze|audit|scan)\s+(?:this|the)\s+(?:apk|app|attachment)[.!?]*$", low))


def prepare_turn_attachments(
    attachment_ids: Optional[Iterable[str]],
    *,
    project_root: Optional[str | Path] = None,
    message: str = "",
) -> dict[str, Any]:
    """Build dynamic context string, APK inspect targets, and activity events."""
    ids = [str(a).strip() for a in (attachment_ids or []) if str(a or "").strip()][:5]
    if not ids:
        return {"dynamic_context": "", "direct_inspect_apk": None, "activities": [], "attachments": []}

    blocks: list[str] = []
    activities: list[dict[str, Any]] = []
    found: list[dict[str, Any]] = []
    direct_inspect_apk: Optional[dict[str, Any]] = None

    for aid in ids:
        meta = get_attachment(aid, project_root=project_root)
        if meta is None:
            continue
        found.append(meta)
        kind = meta.get("kind", "binary")
        filename = str(meta.get("filename") or "attachment")
        mime_type = str(meta.get("mime_type") or "application/octet-stream")
        size_bytes = int(meta.get("size_bytes") or 0)
        fpath = Path(meta["path"])

        if kind == "text":
            t0 = time.perf_counter()
            raw = fpath.read_text(encoding="utf-8", errors="replace")
            truncated = len(raw) > MAX_TEXT_EXCERPT_CHARS
            excerpt = raw[:MAX_TEXT_EXCERPT_CHARS]
            notice = (
                f"\n[Truncated at {MAX_TEXT_EXCERPT_CHARS} of {len(raw)} chars]"
                if truncated else ""
            )
            blocks.append(
                f"--- ATTACHED FILE: {filename} ({mime_type}, {_format_size(size_bytes)}) ---\n"
                f"```\n{excerpt}\n```{notice}\n"
                f"--- END ATTACHED FILE: {filename} ---"
            )
            activities.append({
                "stage": "attachment",
                "label": f"Attached {filename}",
                "detail": f"{_format_size(size_bytes)} · {mime_type}",
                "duration_ms": round((time.perf_counter() - t0) * 1000, 1),
            })
        elif kind == "apk":
            if direct_inspect_apk is None and should_direct_inspect_apk(message):
                direct_inspect_apk = meta
            else:
                t0 = time.perf_counter()
                try:
                    from . import inspect as rev

                    summary = rev.inspect_apk(str(fpath))
                except Exception as exc:
                    summary = f"APK inspection error: {type(exc).__name__}"
                summary_trim = summary[:MAX_TEXT_EXCERPT_CHARS]
                if len(summary) > MAX_TEXT_EXCERPT_CHARS:
                    summary_trim += f"\n[Truncated at {MAX_TEXT_EXCERPT_CHARS} chars]"
                blocks.append(
                    f"--- ATTACHED APK ANALYSIS: {filename} ({_format_size(size_bytes)}) ---\n"
                    f"{summary_trim}\n"
                    f"--- END ATTACHED APK ANALYSIS: {filename} ---"
                )
                activities.append({
                    "stage": "attachment",
                    "label": f"Analyzed APK {filename}",
                    "detail": _format_size(size_bytes),
                    "duration_ms": round((time.perf_counter() - t0) * 1000, 1),
                })
        else:
            dims_note = (
                f", dimensions {meta['width']}x{meta['height']}"
                if meta.get("width") and meta.get("height") else ""
            )
            blocks.append(
                f"[Attached {kind} file: {filename} ({mime_type}, {_format_size(size_bytes)}{dims_note}) — "
                "binary contents omitted for text model]"
            )
            activities.append({
                "stage": "attachment",
                "label": f"Attached {filename}",
                "detail": f"{kind} · {_format_size(size_bytes)}{dims_note}",
                "duration_ms": 0.0,
            })

    return {
        "dynamic_context": "\n\n".join(blocks),
        "direct_inspect_apk": direct_inspect_apk,
        "activities": activities,
        "attachments": found,
    }
