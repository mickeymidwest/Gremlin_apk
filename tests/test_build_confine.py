"""write_new_files must never write outside the target project folder,
even if a file block carries a traversal path."""
import asyncio
from pathlib import Path

from gremlin_core import build_project


def test_write_new_files_confines_to_root(tmp_path):
    root = tmp_path / "proj"
    root.mkdir()
    files = {"ok.txt": "hi", "../escape.txt": "should not be written",
             ".": "degenerate", "": "degenerate"}  # must not crash on these
    asyncio.run(build_project.write_new_files(files, str(root), "goal", "test", run_tests=False))
    assert (root / "ok.txt").read_text() == "hi"
    assert not (tmp_path / "escape.txt").exists()   # .. never escaped the folder
