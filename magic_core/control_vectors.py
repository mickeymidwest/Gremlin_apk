"""Runtime GGUF control-vector loader, validator, and R5 cache fingerprinting."""
from __future__ import annotations

import ctypes
import hashlib
import math
import re
import struct
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

MAX_ALPHA = 1.5

_GGUF_MAGIC = b"GGUF"
_GGML_TYPE_F32 = 0
_GGML_TYPE_F16 = 1

# GGUF scalar metadata value sizes (bytes)
_GGUF_SCALAR_SIZES: dict[int, int] = {
    0: 1,  # UINT8
    1: 1,  # INT8
    2: 2,  # UINT16
    3: 2,  # INT16
    4: 4,  # UINT32
    5: 4,  # INT32
    6: 4,  # FLOAT32
    7: 1,  # BOOL
    10: 8,  # UINT64
    11: 8,  # INT64
    12: 8,  # FLOAT64
}


@dataclass
class ControlVectorData:
    path: str
    n_embd: int
    max_layer: int
    layers: dict[int, list[float]]
    model_hint: str = ""
    file_sha256: str = ""


def _read_exact(f, n: int) -> bytes:
    buf = f.read(n)
    if len(buf) != n:
        raise ValueError("Unexpected EOF while reading GGUF")
    return buf


def _read_str(f) -> str:
    (length,) = struct.unpack("<Q", _read_exact(f, 8))
    if length > 16 * 1024 * 1024:
        raise ValueError(f"GGUF string too large: {length}")
    return _read_exact(f, length).decode("utf-8", errors="replace")


def _read_kv_value(f, vtype: int) -> Any:
    if vtype == 8:  # STRING
        return _read_str(f)
    if vtype == 9:  # ARRAY
        elem_type, count = struct.unpack("<IQ", _read_exact(f, 12))
        if elem_type in _GGUF_SCALAR_SIZES:
            _read_exact(f, _GGUF_SCALAR_SIZES[elem_type] * count)
            return None
        for _ in range(count):
            _read_kv_value(f, elem_type)
        return None
    if vtype == 4:  # UINT32
        return struct.unpack("<I", _read_exact(f, 4))[0]
    if vtype == 5:  # INT32
        return struct.unpack("<i", _read_exact(f, 4))[0]
    if vtype == 6:  # FLOAT32
        return struct.unpack("<f", _read_exact(f, 4))[0]
    if vtype == 7:  # BOOL
        return bool(struct.unpack("<B", _read_exact(f, 1))[0])
    sz = _GGUF_SCALAR_SIZES.get(vtype)
    if sz is None:
        raise ValueError(f"Unsupported GGUF metadata value type: {vtype}")
    _read_exact(f, sz)
    return None


def _parse_direction_layer(name: str) -> int:
    if not name.startswith("direction."):
        raise ValueError(f"Invalid control-vector tensor name {name!r} (expected 'direction.<layer>')")
    suffix = name.split(".", 1)[1]
    try:
        layer_idx = int(suffix)
    except ValueError as exc:
        raise ValueError(f"Invalid direction layer index in tensor {name!r}") from exc
    if layer_idx <= 0:
        raise ValueError(f"Invalid direction layer index {layer_idx} in {name!r} (must be >= 1)")
    return layer_idx


def _read_gguf_stdlib(path: Path) -> ControlVectorData:
    """Minimal stdlib GGUF v2/v3 reader for 1-D F32/F16 `direction.<layer>` tensors."""
    with path.open("rb") as f:
        magic = _read_exact(f, 4)
        if magic != _GGUF_MAGIC:
            raise ValueError(f"{path} is not a GGUF file (bad magic {magic!r})")
        (version,) = struct.unpack("<I", _read_exact(f, 4))
        if version not in (2, 3):
            raise ValueError(f"Unsupported GGUF version {version} in {path} (expected 2 or 3)")
        n_tensors, n_kv = struct.unpack("<QQ", _read_exact(f, 16))

        alignment = 32
        model_hint = ""
        for _ in range(n_kv):
            key = _read_str(f)
            (vtype,) = struct.unpack("<I", _read_exact(f, 4))
            val = _read_kv_value(f, vtype)
            if key == "general.alignment" and isinstance(val, int) and val > 0:
                alignment = val
            elif key == "controlvector.model_hint" and isinstance(val, str):
                model_hint = val.strip()

        tensor_infos: list[tuple[str, int, list[int], int, int]] = []
        for _ in range(n_tensors):
            tname = _read_str(f)
            (n_dims,) = struct.unpack("<I", _read_exact(f, 4))
            dims = list(struct.unpack(f"<{n_dims}Q", _read_exact(f, 8 * n_dims)))
            ggml_type, offset = struct.unpack("<IQ", _read_exact(f, 12))
            tensor_infos.append((tname, n_dims, dims, ggml_type, offset))

        pos = f.tell()
        data_start = ((pos + alignment - 1) // alignment) * alignment

        layers: dict[int, list[float]] = {}
        n_embd: Optional[int] = None
        for tname, n_dims, dims, ggml_type, offset in tensor_infos:
            layer_idx = _parse_direction_layer(tname)
            if n_dims != 1 or not dims or dims[0] <= 0:
                raise ValueError(f"Tensor {tname!r} in {path} must be 1-D, got dims={dims}")
            if ggml_type not in (_GGML_TYPE_F32, _GGML_TYPE_F16):
                raise ValueError(f"Tensor {tname!r} in {path} must be F32 or F16 (got type={ggml_type})")
            dim = int(dims[0])
            if n_embd is None:
                n_embd = dim
            elif dim != n_embd:
                raise ValueError(f"Tensor {tname!r} n_embd={dim} mismatches prior n_embd={n_embd}")

            f.seek(data_start + offset)
            elem_size = 4 if ggml_type == _GGML_TYPE_F32 else 2
            fmt = f"<{dim}f" if ggml_type == _GGML_TYPE_F32 else f"<{dim}e"
            raw = _read_exact(f, dim * elem_size)
            vals = [float(x) for x in struct.unpack(fmt, raw)]
            layers[layer_idx] = vals

    if not layers or n_embd is None:
        raise ValueError(f"No direction.<layer> tensors found in {path}")
    sha = hashlib.sha256(path.read_bytes()).hexdigest()
    return ControlVectorData(
        path=str(path),
        n_embd=n_embd,
        max_layer=max(layers.keys()),
        layers=layers,
        model_hint=model_hint,
        file_sha256=sha,
    )


def _read_gguf_via_pkg(path: Path) -> ControlVectorData:
    import gguf  # type: ignore

    reader = gguf.GGUFReader(str(path), "r")
    model_hint = ""
    field = reader.fields.get("controlvector.model_hint")
    if field is not None and getattr(field, "parts", None):
        try:
            model_hint = bytes(field.parts[-1]).decode("utf-8", errors="replace").strip()
        except Exception:
            model_hint = ""

    layers: dict[int, list[float]] = {}
    n_embd: Optional[int] = None
    for tensor in reader.tensors:
        layer_idx = _parse_direction_layer(tensor.name)
        shape = [int(d) for d in tensor.shape]
        if len(shape) != 1 or shape[0] <= 0:
            raise ValueError(f"Tensor {tensor.name!r} in {path} must be 1-D, got shape={shape}")
        if int(tensor.tensor_type) not in (_GGML_TYPE_F32, _GGML_TYPE_F16):
            raise ValueError(
                f"Tensor {tensor.name!r} in {path} must be F32 or F16 (got type={tensor.tensor_type})"
            )
        dim = shape[0]
        if n_embd is None:
            n_embd = dim
        elif dim != n_embd:
            raise ValueError(f"Tensor {tensor.name!r} n_embd={dim} mismatches prior n_embd={n_embd}")
        vals = [float(x) for x in tensor.data.astype("float32").tolist()]
        layers[layer_idx] = vals

    if not layers or n_embd is None:
        raise ValueError(f"No direction.<layer> tensors found in {path}")
    sha = hashlib.sha256(path.read_bytes()).hexdigest()
    return ControlVectorData(
        path=str(path),
        n_embd=n_embd,
        max_layer=max(layers.keys()),
        layers=layers,
        model_hint=model_hint,
        file_sha256=sha,
    )


def load_cvec_gguf(path: str | Path, *, use_gguf_pkg: bool = True) -> ControlVectorData:
    """Load and validate a `.cvec.gguf` control-vector file."""
    p = Path(path).expanduser().resolve()
    if not p.is_file():
        raise FileNotFoundError(f"Control vector file not found: {path}")
    if use_gguf_pkg:
        try:
            import gguf  # noqa: F401
            return _read_gguf_via_pkg(p)
        except ImportError:
            pass
    return _read_gguf_stdlib(p)


def write_cvec_gguf(
    path: str | Path,
    directions: dict[int, list[float] | tuple[float, ...]],
    *,
    model_hint: str = "",
) -> Path:
    """Write a `llama.cpp`-compatible GGUF v3 control vector (`direction.<layer>`, F32)."""
    if not directions:
        raise ValueError("directions must be non-empty")
    ordered = sorted((int(k), [float(x) for x in v]) for k, v in directions.items())
    if ordered[0][0] <= 0:
        raise ValueError(f"Control vector layer index must be >= 1, got {ordered[0][0]}")
    n_embd = len(ordered[0][1])
    if n_embd <= 0:
        raise ValueError("Control vector n_embd must be > 0")
    for il, vec in ordered:
        if len(vec) != n_embd:
            raise ValueError(f"Layer {il} length {len(vec)} != expected n_embd={n_embd}")

    out_path = Path(path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    alignment = 32

    def _pack_str(s: str) -> bytes:
        b = s.encode("utf-8")
        return struct.pack("<Q", len(b)) + b

    kvs: list[bytes] = [
        _pack_str("general.architecture") + struct.pack("<I", 8) + _pack_str("controlvector"),
        _pack_str("controlvector.layer_count") + struct.pack("<Ii", 5, len(ordered)),
    ]
    if model_hint:
        kvs.append(
            _pack_str("controlvector.model_hint") + struct.pack("<I", 8) + _pack_str(model_hint)
        )

    tensor_infos: list[bytes] = []
    data_chunks: list[bytes] = []
    offset = 0
    for il, vec in ordered:
        tname = f"direction.{il}"
        tensor_infos.append(
            _pack_str(tname)
            + struct.pack("<IQ", 1, n_embd)
            + struct.pack("<IQ", _GGML_TYPE_F32, offset)
        )
        raw = struct.pack(f"<{n_embd}f", *vec)
        pad_len = (-len(raw)) % alignment
        data_chunks.append(raw + (b"\x00" * pad_len))
        offset += len(raw) + pad_len

    header = struct.pack("<4sIQQ", _GGUF_MAGIC, 3, len(ordered), len(kvs))
    meta_blob = header + b"".join(kvs) + b"".join(tensor_infos)
    head_pad = (-len(meta_blob)) % alignment
    out_path.write_bytes(meta_blob + (b"\x00" * head_pad) + b"".join(data_chunks))
    return out_path


def clamp_alpha(alpha: float) -> tuple[float, str]:
    try:
        val = float(alpha)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"Invalid alpha {alpha!r}: must be a float") from exc
    if math.isnan(val) or math.isinf(val):
        raise ValueError(f"Invalid alpha {alpha!r}: must be finite")
    if abs(val) > MAX_ALPHA:
        clamped = MAX_ALPHA if val > 0 else -MAX_ALPHA
        return clamped, (
            f"alpha {val} clamped to {clamped} (|alpha| > {MAX_ALPHA} risks output breakdown)"
        )
    return val, ""


def default_layer_range(n_layers: int, max_cvec_layer: Optional[int] = None) -> tuple[int, int]:
    if n_layers < 2:
        raise ValueError(f"Model must have >= 2 layers for control vectors, got {n_layers}")
    il_start = max(1, n_layers // 3)
    il_end = min(n_layers - 1, (5 * n_layers) // 6)
    if max_cvec_layer is not None:
        il_end = min(il_end, int(max_cvec_layer))
    if il_start > il_end:
        il_start = max(1, il_end)
    return il_start, il_end


def resolve_layer_range(
    n_layers: int,
    il_start: Optional[int] = None,
    il_end: Optional[int] = None,
    *,
    max_cvec_layer: Optional[int] = None,
) -> tuple[int, int]:
    d_start, d_end = default_layer_range(n_layers, max_cvec_layer=max_cvec_layer)
    s = d_start if il_start is None else int(il_start)
    e = d_end if il_end is None else int(il_end)
    if s < 1 or e > n_layers - 1 or s > e:
        raise ValueError(
            f"Invalid layer range {s}-{e} for {n_layers}-layer model (valid: 1..{n_layers - 1})"
        )
    if max_cvec_layer is not None and e > max_cvec_layer:
        raise ValueError(
            f"Layer end {e} exceeds control vector max layer {max_cvec_layer}"
        )
    return s, e


def parse_layer_spec(spec: str) -> tuple[int, int]:
    m = re.fullmatch(r"\s*(\d+)\s*(?:-|:|\.\.)\s*(\d+)\s*", spec or "")
    if not m:
        raise ValueError(f"Invalid layer range {spec!r} (expected e.g. '10-25')")
    return int(m.group(1)), int(m.group(2))


def validate_for_model(
    cvec: ControlVectorData,
    *,
    model_n_embd: int,
    model_arch: str = "",
) -> None:
    if cvec.n_embd != int(model_n_embd):
        hint = f" ({cvec.model_hint})" if cvec.model_hint else ""
        arch = f" ({model_arch})" if model_arch else ""
        raise ValueError(
            f"Control vector n_embd={cvec.n_embd}{hint} does not match "
            f"model n_embd={model_n_embd}{arch}"
        )
    hint_l = (cvec.model_hint or "").strip().lower()
    arch_l = (model_arch or "").strip().lower()
    if hint_l and arch_l and hint_l != "controlvector" and hint_l != arch_l:
        raise ValueError(
            f"Control vector architecture {cvec.model_hint!r} does not match model architecture {model_arch!r}"
        )


def build_scaled_buffer(
    cvec: ControlVectorData,
    alpha: float,
    *,
    n_layers: Optional[int] = None,
) -> tuple[Any, int]:
    """Pack scaled `direction.1..max_layer` floats into a contiguous ctypes c_float array."""
    span_layers = max(cvec.max_layer, (int(n_layers) - 1) if n_layers else cvec.max_layer)
    total_len = span_layers * cvec.n_embd
    buf = (ctypes.c_float * total_len)()
    scale = float(alpha)
    for il, vec in cvec.layers.items():
        if il < 1 or il > span_layers:
            continue
        base = (il - 1) * cvec.n_embd
        for j, val in enumerate(vec):
            buf[base + j] = val * scale
    return buf, total_len


def cache_fingerprint(
    path: Optional[str],
    alpha: float,
    il_start: int,
    il_end: int,
    *,
    file_sha256: str = "",
) -> str:
    if not path or float(alpha) == 0.0:
        return "none"
    p_hash = file_sha256
    if not p_hash:
        p = Path(path)
        if p.is_file():
            p_hash = hashlib.sha256(p.read_bytes()).hexdigest()[:16]
        else:
            p_hash = hashlib.sha256(str(path).encode("utf-8")).hexdigest()[:16]
    else:
        p_hash = p_hash[:16]
    payload = f"{p_hash}:{float(alpha):.6f}:{int(il_start)}:{int(il_end)}"
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:12]


def cvecs_dir(project_root: str | Path) -> Path:
    return Path(project_root) / "models" / "cvecs"


def list_cvecs(project_root: str | Path) -> list[Path]:
    d = cvecs_dir(project_root)
    if not d.is_dir():
        return []
    return sorted(p for p in d.glob("*.cvec.gguf") if p.is_file())


def resolve_cvec_path(name_or_path: str, project_root: str | Path) -> Path:
    raw = str(name_or_path or "").strip()
    if not raw:
        raise FileNotFoundError("Empty control vector name or path")
    direct = Path(raw).expanduser()
    if direct.is_file():
        return direct.resolve()
    if "/" not in raw and "\\" not in raw and ".." not in Path(raw).parts:
        base_dir = cvecs_dir(project_root).resolve()
        for cand_name in (raw, f"{raw}.cvec.gguf"):
            cand = (base_dir / cand_name).resolve()
            if cand.parent == base_dir and cand.is_file():
                return cand
    raise FileNotFoundError(
        f"Control vector not found: {raw!r} (checked path and {cvecs_dir(project_root)})"
    )
