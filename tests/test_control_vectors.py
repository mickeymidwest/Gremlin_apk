"""Offline unit tests for GGUF control-vector steering, R5 isolation, and /steer."""
from __future__ import annotations

import asyncio
import ctypes
import struct
from pathlib import Path
from types import SimpleNamespace

import pytest

from magic_core import control_vectors as cv, prefix_cache, server
from magic_core.backends.base import ModelInfo
from magic_core.backends.llamacpp_backend import LlamaCppBackend
from magic_core.magic import commands
from magic_core.persona import PersonaBackend
from magic_core.router import Router


def _write_f16_gguf(path: Path, directions: dict[int, list[float]], *, model_hint: str = "") -> None:
    """Write a minimal GGUF v3 file with F16 1-D `direction.<layer>` tensors."""
    alignment = 32

    def _s(txt: str) -> bytes:
        b = txt.encode("utf-8")
        return struct.pack("<Q", len(b)) + b

    kvs = [_s("general.architecture") + struct.pack("<I", 8) + _s("controlvector")]
    if model_hint:
        kvs.append(_s("controlvector.model_hint") + struct.pack("<I", 8) + _s(model_hint))

    ordered = sorted(directions.items())
    tinfos = []
    chunks = []
    off = 0
    for il, vec in ordered:
        tinfos.append(_s(f"direction.{il}") + struct.pack("<IQ", 1, len(vec)) + struct.pack("<IQ", 1, off))
        raw = struct.pack(f"<{len(vec)}e", *vec)
        pad = (-len(raw)) % alignment
        chunks.append(raw + (b"\x00" * pad))
        off += len(raw) + pad

    blob = struct.pack("<4sIQQ", b"GGUF", 3, len(ordered), len(kvs)) + b"".join(kvs) + b"".join(tinfos)
    hpad = (-len(blob)) % alignment
    path.write_bytes(blob + (b"\x00" * hpad) + b"".join(chunks))


def test_gguf_reader_f32_f16_and_validation(tmp_path):
    p_f32 = tmp_path / "vec_f32.cvec.gguf"
    dirs = {1: [1.0, 0.0, -0.5, 0.25], 2: [0.0, 0.5, 0.5, -1.0], 3: [0.2, 0.4, 0.6, 0.8]}
    cv.write_cvec_gguf(p_f32, dirs, model_hint="gemma4")

    # Test both stdlib reader and gguf-package reader
    loaded_std = cv.load_cvec_gguf(p_f32, use_gguf_pkg=False)
    loaded_pkg = cv.load_cvec_gguf(p_f32, use_gguf_pkg=True)
    for loaded in (loaded_std, loaded_pkg):
        assert loaded.n_embd == 4
        assert loaded.max_layer == 3
        assert loaded.model_hint == "gemma4"
        assert set(loaded.layers.keys()) == {1, 2, 3}
        assert loaded.layers[1] == pytest.approx(dirs[1], abs=1e-6)

    # F16 reader via stdlib struct
    p_f16 = tmp_path / "vec_f16.cvec.gguf"
    _write_f16_gguf(p_f16, dirs, model_hint="gemma4")
    loaded_f16 = cv.load_cvec_gguf(p_f16, use_gguf_pkg=False)
    assert loaded_f16.n_embd == 4
    assert loaded_f16.layers[2] == pytest.approx(dirs[2], abs=1e-2)

    # n_embd and architecture mismatch rejection
    cv.validate_for_model(loaded_std, model_n_embd=4, model_arch="gemma4")
    with pytest.raises(ValueError, match="n_embd"):
        cv.validate_for_model(loaded_std, model_n_embd=3840, model_arch="gemma4")
    with pytest.raises(ValueError, match="architecture"):
        cv.validate_for_model(loaded_std, model_n_embd=4, model_arch="qwen2")

    # Layer 0 rejected
    p_bad = tmp_path / "bad_layer0.cvec.gguf"
    _write_f16_gguf(p_bad, {0: [1.0, 2.0], 1: [3.0, 4.0]})
    with pytest.raises(ValueError, match="layer index"):
        cv.load_cvec_gguf(p_bad, use_gguf_pkg=False)


def test_alpha_clamp_layer_bounds_and_c_api_calls(tmp_path, monkeypatch):
    vec_path = tmp_path / "directness.cvec.gguf"
    dirs = {il: [float(il), 1.0, -1.0, 0.5] for il in range(1, 30)}  # 30-layer model
    cv.write_cvec_gguf(vec_path, dirs, model_hint="gemma4")

    c_calls = []

    class _FakeCache:
        def __init__(self):
            self.cache_state = {("a",): "stale"}

    class _FakeLlama:
        def __init__(self):
            self._ctx = SimpleNamespace(ctx=ctypes.c_void_p(0x1234))
            self._model = SimpleNamespace(model=ctypes.c_void_p(0x5678))
            self._n_layer = 30
            self.metadata = {"general.architecture": "gemma4"}
            self.cache = _FakeCache()
            self.reset_calls = 0

        def n_embd(self):
            return 4

        def reset(self):
            self.reset_calls += 1

    def _fake_set_cvec(ctx_ptr, data_ptr, length, n_embd, il_start, il_end):
        snapshot = None
        if data_ptr is not None and length > 0:
            snapshot = [float(data_ptr[i]) for i in range(length)]
        c_calls.append((ctx_ptr.value, snapshot is not None, length, n_embd, il_start, il_end, snapshot))
        return 0

    monkeypatch.setattr(LlamaCppBackend, "_set_adapter_cvec", staticmethod(_fake_set_cvec))

    be = LlamaCppBackend(ModelInfo(name="gemma4-26b-a4b", kind="local_gguf"), model_path=str(tmp_path / "m.gguf"))
    be._llm = _FakeLlama()

    # Over-limit alpha clamps to +-1.5 and warns
    st = be.apply_control_vector(str(vec_path), alpha=2.4)
    assert st["active"] is True
    assert st["alpha"] == 1.5
    assert "clamped" in st["warning"]
    assert st["il_start"] == 10 and st["il_end"] == 25  # 30 // 3 .. (5*30) // 6
    assert be._cvec_keepalive is not None
    assert be._llm.cache.cache_state == {}
    assert be._llm.reset_calls == 1

    ctx_v, has_ptr, length, n_embd, il_s, il_e, snap = c_calls[-1]
    assert ctx_v == 0x1234 and has_ptr is True
    assert length == 29 * 4 and n_embd == 4 and il_s == 10 and il_e == 25
    # Layer 10 occupies index (10 - 1) * 4 = 36..40, scaled by 1.5
    assert snap[36:40] == pytest.approx([15.0, 1.5, -1.5, 0.75], abs=1e-5)

    # Clear passes (None, 0, 0, -1, -1)
    st_off = be.clear_control_vector()
    assert st_off["active"] is False
    assert st_off["alpha"] == 0.0
    assert st_off["signature"] == "none"
    assert be._cvec_keepalive is None
    assert c_calls[-1][:6] == (0x1234, False, 0, 0, -1, -1)

    # n_embd mismatch on apply raises cleanly without mutating active state
    bad_vec = tmp_path / "qwen.cvec.gguf"
    cv.write_cvec_gguf(bad_vec, {1: [1.0] * 8}, model_hint="qwen2")
    with pytest.raises(ValueError, match="n_embd"):
        be.apply_control_vector(str(bad_vec), alpha=0.5)
    assert be.control_vector_signature() == "none"


def test_r5_prefix_cache_isolation_for_control_vectors(tmp_path, monkeypatch):
    monkeypatch.setenv("MAGIC_PREFIX_CACHE", "1")
    prefix_cache.get_prefix_cache().clear()

    gguf_model = tmp_path / "gemma4.gguf"
    gguf_model.write_bytes(b"fake-gguf")
    vec_path = tmp_path / "directness.cvec.gguf"
    cv.write_cvec_gguf(vec_path, {il: [0.5, 0.5, 0.5, 0.5] for il in range(1, 13)}, model_hint="gemma4")

    monkeypatch.setattr(LlamaCppBackend, "_set_adapter_cvec", staticmethod(lambda *a: 0))

    class _DummyLlama:
        def __init__(self):
            self._ctx = SimpleNamespace(ctx=ctypes.c_void_p(0x9999))
            self._model = SimpleNamespace(model=ctypes.c_void_p(0x8888), add_bos_token=lambda: False, token_cls=lambda: -1)
            self._n_layer = 13
            self.metadata = {"general.architecture": "gemma4"}
            self.eval_count = 0

        def n_embd(self):
            return 4

        def reset(self):
            pass

        def tokenize(self, raw, add_bos=False, special=True):
            return list(range(1, 30))

        def eval(self, toks):
            self.eval_count += 1

        def save_state(self):
            return SimpleNamespace(tag=f"state_{self.eval_count}", llama_state_size=1024 * 1024)

        def load_state(self, state):
            self.loaded_tag = state.tag

        def create_completion(self, *a, **kw):
            return {"choices": [{"text": "ok"}]}

    be = LlamaCppBackend(
        ModelInfo(name="gemma4-26b-a4b", kind="local_gguf"),
        model_path=str(gguf_model),
        chat_format="gemma4",
    )
    be._llm = _DummyLlama()

    msgs = [
        {"role": "system", "content": "You are Magic."},
        {"role": "user", "content": "Static profile and skills " * 30},
        {"role": "assistant", "content": "Understood."},
        {"role": "user", "content": "Today's date is 2026-11-24."},
        {"role": "assistant", "content": "Noted."},
        {"role": "user", "content": "Hello"},
    ]

    # 1. Neutral (0.0) misses then hits
    _, p0_a = be._prepare_prefix_and_prompt(msgs)
    _, p0_b = be._prepare_prefix_and_prompt(msgs)
    assert p0_a["prefix_cache_hit"] is False
    assert p0_b["prefix_cache_hit"] is True
    assert be._llm.loaded_tag == "state_1"

    # 2. Steered (alpha=0.4) must NOT hit neutral checkpoint; gets its own entry
    be.apply_control_vector(str(vec_path), alpha=0.4)
    _, p1_a = be._prepare_prefix_and_prompt(msgs)
    _, p1_b = be._prepare_prefix_and_prompt(msgs)
    assert p1_a["prefix_cache_hit"] is False
    assert p1_b["prefix_cache_hit"] is True
    assert be._llm.loaded_tag == "state_2"
    assert prefix_cache.get_prefix_cache().stats()["entries"] == 2

    # 3. Returning to off immediately hits the original neutral checkpoint
    be.clear_control_vector()
    _, p0_c = be._prepare_prefix_and_prompt(msgs)
    assert p0_c["prefix_cache_hit"] is True
    assert be._llm.loaded_tag == "state_1"


def test_steer_command_and_status_endpoint(tmp_path, monkeypatch):
    cvec_dir = tmp_path / "models" / "cvecs"
    cvec_dir.mkdir(parents=True)
    (tmp_path / "config").mkdir()
    (tmp_path / "config" / "models.yaml").write_text(
        (Path(__file__).resolve().parent.parent / "config" / "models.yaml").read_text(encoding="utf-8"),
        encoding="utf-8",
    )
    vec_file = cvec_dir / "gemma4_directness.cvec.gguf"
    cv.write_cvec_gguf(vec_file, {il: [0.1, 0.2, 0.3, 0.4] for il in range(1, 13)}, model_hint="gemma4")

    monkeypatch.setattr(LlamaCppBackend, "_set_adapter_cvec", staticmethod(lambda *a: 0))

    be = LlamaCppBackend(
        ModelInfo(name="gemma4-26b-a4b", kind="local_gguf"),
        model_path=str(tmp_path / "m.gguf"),
        chat_format="gemma4",
    )
    be._llm = SimpleNamespace(
        _ctx=SimpleNamespace(ctx=ctypes.c_void_p(0x7777)),
        _model=SimpleNamespace(model=ctypes.c_void_p(0x6666)),
        _n_layer=13,
        metadata={"general.architecture": "gemma4"},
        n_embd=lambda: 4,
        reset=lambda: None,
    )

    class _Reg:
        def __init__(self):
            self.backends = {
                "gemma4-26b-a4b": be,
                "magic": PersonaBackend(
                    ModelInfo(name="magic", kind="persona", display_name="Magic"),
                    primary=be, fallbacks=[], system_prompt="sys",
                ),
            }

        def get(self, name):
            return self.backends.get(name)

        def primary_model_name(self):
            return "gemma4-26b-a4b"

        def available_names(self):
            return ["gemma4-26b-a4b"]

    reg = _Reg()
    ctx = commands.CommandContext(
        registry=reg, project_root=str(tmp_path), config_path=str(tmp_path / "config" / "models.yaml")
    )

    # /steer lists available vectors and shows neutral status
    r0 = asyncio.run(commands.dispatch("/steer", ctx))
    assert r0["ok"] is True
    assert "off" in r0["answer"]
    assert "gemma4_directness.cvec.gguf" in r0["answer"]

    # /steer <name> <alpha> [layers] activates steering live
    r1 = asyncio.run(commands.dispatch("/steer gemma4_directness 0.5 3-10", ctx))
    assert r1["ok"] is True
    assert r1["control_vector"]["active"] is True
    assert r1["control_vector"]["alpha"] == 0.5
    assert r1["control_vector"]["il_start"] == 3 and r1["control_vector"]["il_end"] == 10

    # /status exposes control_vector while active
    loop = server.start_background_loop()
    try:
        app = server.create_app(reg, Router(reg), tmp_path, loop, token="tok", admin_token="adm")
        client = app.test_client()
        st_active = client.get("/status", headers={"Authorization": "Bearer tok"}).get_json()
        assert st_active["control_vector"]["active"] is True
        assert st_active["control_vector"]["name"] == "gemma4_directness.cvec.gguf"
        assert st_active["control_vector"]["alpha"] == 0.5

        # /steer off clears it and /status reflects neutral
        r2 = asyncio.run(commands.dispatch("/steer off", ctx))
        assert r2["ok"] is True
        assert r2["control_vector"]["active"] is False
        st_off = client.get("/status", headers={"Authorization": "Bearer tok"}).get_json()
        assert st_off["control_vector"]["active"] is False
        assert st_off["control_vector"]["signature"] == "none"
    finally:
        loop.call_soon_threadsafe(loop.stop)
