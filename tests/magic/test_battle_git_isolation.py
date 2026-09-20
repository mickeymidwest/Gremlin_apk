"""_git_begin -- real bug found live 2026-09-19: every battle workdir is
a subdirectory of THIS project's own data/magic/work/, itself inside
the real gremlin git repo. The old check ("rev-parse
--is-inside-work-tree") is true for any subdirectory of an existing
repo, including one it doesn't own -- so it always skipped `git init`
and every per-edit autocommit (_git_snapshot) landed straight in the
REAL repo's history instead of an isolated one. 108 junk commits deep
by the time this was caught, 99 already pushed. These tests exercise
the exact real shape: a battle workdir NESTED inside a parent repo,
not a standalone tmp_path (which is why the existing test_battle.py
coverage never caught this -- pytest's tmp_path isn't inside any git
repo, so the bug's precondition never applied there)."""
import subprocess

from gremlin_core.magic.battle import _git, _git_begin, _git_snapshot


def _init_outer_repo(root):
    subprocess.run(["git", "init", "-q"], cwd=root, capture_output=True)
    subprocess.run(["git", "-c", "user.email=x@x", "-c", "user.name=x",
                    "commit", "-q", "--allow-empty", "-m", "outer: initial"],
                   cwd=root, capture_output=True)


def test_battle_inside_a_parent_repo_gets_its_own_isolated_repo(tmp_path):
    outer = tmp_path / "outer_repo"
    outer.mkdir()
    _init_outer_repo(outer)

    battle = outer / "data" / "magic" / "work" / "battle_0001_x"
    battle.mkdir(parents=True)
    (battle / "file.py").write_text("x = 1\n")

    assert _git_begin(str(battle)) is True

    # the battle dir must be its OWN repo root now, not just "inside" the outer one
    top = _git(str(battle), "rev-parse", "--show-toplevel").stdout.strip()
    import os
    assert os.path.realpath(top) == os.path.realpath(str(battle))


def test_battle_commits_never_land_in_the_outer_repo(tmp_path):
    outer = tmp_path / "outer_repo"
    outer.mkdir()
    _init_outer_repo(outer)

    battle = outer / "data" / "magic" / "work" / "battle_0002_y"
    battle.mkdir(parents=True)
    (battle / "file.py").write_text("y = 1\n")

    _git_begin(str(battle))
    _git_snapshot(str(battle), "step 1: edited file.py")

    outer_log = subprocess.run(["git", "log", "--oneline"], cwd=outer,
                               capture_output=True, text=True).stdout
    assert "step 1" not in outer_log
    assert "magic: battle start" not in outer_log
    assert "outer: initial" in outer_log  # the outer repo's own history is untouched

    battle_log = _git(str(battle), "log", "--oneline").stdout
    assert "step 1" in battle_log
    assert "magic: battle start" in battle_log


def test_battle_still_works_when_not_nested_in_any_repo(tmp_path):
    # the original, already-covered case (test_battle.py's own
    # run_battle test) -- must keep working, not just the nested one
    standalone = tmp_path / "standalone"
    standalone.mkdir()
    (standalone / "file.py").write_text("z = 1\n")

    assert _git_begin(str(standalone)) is True
    log = _git(str(standalone), "log", "--oneline").stdout
    assert "magic: battle start" in log
