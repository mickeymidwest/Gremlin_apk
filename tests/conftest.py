"""Shared test fixtures."""
import pytest


@pytest.fixture(autouse=True)
def _isolate_memory(tmp_path, monkeypatch):
    """Each test gets its own magic_memory.txt. Without this, the
    default path (one level up from the repo root) resolves to the
    shared pytest tmp dir and tests leak facts into each other -- see
    magic_core/notes.memory_file_path."""
    monkeypatch.setenv("MAGIC_MEMORY_FILE", str(tmp_path / "magic_memory.txt"))
    # same for How Mickey Likes Things (magic_core/profile.py) -- the
    # default path would be the real ~/Downloads/gremlin_profile.md
    monkeypatch.setenv("MAGIC_PROFILE_FILE", str(tmp_path / "magic_profile.md"))
