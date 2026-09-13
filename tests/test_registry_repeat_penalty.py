"""config/models.yaml's `repeat_penalty` field actually reaches the
backend -- the missing link between the LlamaCppBackend default fix
(tests/test_llamacpp_backend.py) and a real per-model config override."""
import gremlin_core.backends.llamacpp_backend as backend_mod
from gremlin_core.registry import ModelRegistry


class _FakeLlm:
    def __init__(self, **kwargs):
        pass

    def create_chat_completion(self, **kwargs):
        return {"choices": [{"message": {"content": "ok"}}]}


def test_repeat_penalty_flows_from_config_to_backend(tmp_path, monkeypatch):
    monkeypatch.setattr(backend_mod, "Llama", lambda **kw: _FakeLlm(**kw))
    cfg = tmp_path / "models.yaml"
    cfg.write_text(
        "models:\n"
        "  - name: m\n"
        "    type: local_gguf\n"
        "    model_path: /fake/m.gguf\n"
        "    repeat_penalty: 1.25\n"
        "persona:\n"
        "  name: gremlin\n"
        "  primary_model: m\n"
    )
    reg = ModelRegistry.from_yaml(str(cfg))
    assert reg.get("m").repeat_penalty == 1.25


def test_repeat_penalty_defaults_when_not_in_config(tmp_path, monkeypatch):
    monkeypatch.setattr(backend_mod, "Llama", lambda **kw: _FakeLlm(**kw))
    cfg = tmp_path / "models.yaml"
    cfg.write_text(
        "models:\n"
        "  - name: m\n"
        "    type: local_gguf\n"
        "    model_path: /fake/m.gguf\n"
        "persona:\n"
        "  name: gremlin\n"
        "  primary_model: m\n"
    )
    reg = ModelRegistry.from_yaml(str(cfg))
    assert reg.get("m").repeat_penalty == 1.1
