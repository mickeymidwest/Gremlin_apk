"""best_coder_model: pick the strongest local coder for self-authoring."""
from magic_core import tool_forge as TF


class _Reg:
    def __init__(self, names, missing=()):
        self.raw_config = {"models": [{"name": n} for n in names]}
        self._missing = set(missing)

    def get(self, n):
        return None if n in self._missing else object()

    def primary_model_name(self):
        return "qwen3.5-9b"


def test_prefers_a_coder_model():
    r = _Reg(["qwen3.5-9b", "qwen2.5-14b", "qwen2.5-coder-14b", "gemini"], missing=["gemini"])
    assert TF.best_coder_model(r) == "qwen2.5-coder-14b"


def test_falls_back_to_a_big_general_model():
    r = _Reg(["qwen3.5-9b", "qwen2.5-14b", "gemini"], missing=["gemini"])
    assert TF.best_coder_model(r) == "qwen2.5-14b"


def test_none_when_only_small_or_remote():
    r = _Reg(["qwen3.5-9b", "gemini"], missing=["gemini"])
    assert TF.best_coder_model(r) is None


def test_skips_abliterated_and_missing():
    # the 14b-abliterated is skipped (tuned for freedom, not clean syntax);
    # a coder that isn't locally present is skipped too
    r = _Reg(["qwen2.5-coder-14b", "qwen2.5-14b-abliterated", "qwen3.5-9b"],
             missing=["qwen2.5-coder-14b"])
    assert TF.best_coder_model(r) == "qwen2.5-14b" or TF.best_coder_model(r) is None
