"""build_entry_block_hf -- the /model download registration block, which
(unlike the older local-folder-scan build_entry_block) carries the
fields the VRAM governor and rest of the harness actually read."""
from gremlin_core import model_scan


def test_build_entry_block_hf_carries_governor_fields():
    block = model_scan.build_entry_block_hf(
        "some-7b", "/home/mickey/Downloads/gremlin/models/some-7b.gguf",
        "some-7b.Q4_K_M.gguf", footprint_mb=5600, n_ctx=8192)
    assert "name: some-7b" in block
    assert 'model_path: "/home/mickey/Downloads/gremlin/models/some-7b.gguf"' in block
    assert "footprint_mb: 5600" in block
    assert "flash_attn: true" in block
    assert "kv_cache_type: q8_0" in block  # q4_0 confirmed to degenerate Qwen2.5-family GGUFs
    assert "n_ctx: 8192" in block


def test_build_entry_block_hf_is_registerable(tmp_path):
    cfg = tmp_path / "models.yaml"
    cfg.write_text("models:\n\npersona:\n  name: gremlin\n  primary_model: x\n")
    block = model_scan.build_entry_block_hf("y", "/tmp/y.gguf", "y.gguf", footprint_mb=2800)
    model_scan.insert_entries(str(cfg), [block])
    entries = model_scan.list_all_entries(cfg.read_text())
    assert entries and entries[0]["name"] == "y" and entries[0]["footprint_mb"] == 2800
