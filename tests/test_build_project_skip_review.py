"""skip_review -- mickey (2026-09-13) decided build_project's two-
reviewer gate isn't worth the friction (it only ever writes to a
throwaway, revertible ~/Downloads/ git repo), unlike self_improve.
run_self_edit rewriting Gremlin's own live source, which keeps it.
When skip_review=True, review_mod.review_and_revise must never be
called at all."""
import asyncio

import gremlin_core.build_project as build_project


def test_skip_review_never_calls_the_review_gate(tmp_path, monkeypatch):
    called = []
    monkeypatch.setattr(build_project.review_mod, "review_and_revise",
                        lambda *a, **kw: called.append(1))

    async def _fake_write_new_files(files, target_root, goal, applied_by, run_tests, router):
        return {"applied": True, "committed": True, "commit_message": "test",
                "files_changed": list(files.keys())}

    monkeypatch.setattr(build_project, "write_new_files", _fake_write_new_files)
    monkeypatch.setattr(build_project, "parse_file_blocks", lambda patch: {"main.py": "print(1)"})

    target = str(tmp_path / "proj")
    result = asyncio.run(build_project.run_build(
        router=None, gremlin_root=str(tmp_path), target_root=target,
        goal="a thing", model_names=["m"], patch="FILE: main.py\nprint(1)",
        skip_review=True,
    ))

    assert not called  # review_and_revise never invoked
    assert result["applied"] is True
    assert result["review_history"] == []  # empty history, not a fabricated one


def test_review_still_runs_when_not_skipped(tmp_path, monkeypatch):
    from gremlin_core.review import ReviewOutcome

    called = []

    async def _fake_review(*a, **kw):
        called.append(1)
        return ReviewOutcome(approved=True, patch=kw.get("patch") if "patch" in kw else a[1],
                             rounds_used=1, history=[])

    monkeypatch.setattr(build_project.review_mod, "review_and_revise", _fake_review)

    async def _fake_write_new_files(files, target_root, goal, applied_by, run_tests, router):
        return {"applied": True, "committed": True, "commit_message": "test",
                "files_changed": list(files.keys())}

    monkeypatch.setattr(build_project, "write_new_files", _fake_write_new_files)
    monkeypatch.setattr(build_project, "parse_file_blocks", lambda patch: {"main.py": "print(1)"})

    target = str(tmp_path / "proj2")
    result = asyncio.run(build_project.run_build(
        router=None, gremlin_root=str(tmp_path), target_root=target,
        goal="a thing", model_names=["m"], patch="FILE: main.py\nprint(1)",
        skip_review=False,
    ))

    assert called  # review_and_revise WAS invoked
    assert result["applied"] is True
