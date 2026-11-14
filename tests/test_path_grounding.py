"""Path grounding: a user-named absolute/home path that doesn't exist is
caught before the classifier can pivot to a different check."""
import os

from gremlin_core.magic import path_grounding as pg


def test_candidates_extracts_abs_paths_only():
    c = pg._candidates("check /home/robofuse-stack and ~/notes.txt please")
    assert "/home/robofuse-stack" in c
    assert "~/notes.txt" in c


def test_candidates_skips_urls_and_idioms():
    c = pg._candidates("see https://example.com/a/b and the and/or case, 24/7")
    assert c == []


def test_missing_path_reported_with_nearest(tmp_path):
    # fake a /home with the real stack one level deeper under a "user" dir
    home = tmp_path / "home" / "mickey"
    (home / "robofuse-stack").mkdir(parents=True)
    msg = f"check {tmp_path}/home/robofuse-stack for problems"
    missing = pg.check_user_paths(msg, home=str(home))
    assert len(missing) == 1
    assert missing[0]["path"] == f"{tmp_path}/home/robofuse-stack"
    assert missing[0]["nearest"] == str(home / "robofuse-stack")


def test_existing_path_not_flagged(tmp_path):
    real = tmp_path / "data"
    real.mkdir()
    missing = pg.check_user_paths(f"look at {real}", home=str(tmp_path))
    assert missing == []


def test_short_circuit_only_on_inspect(tmp_path):
    missing = [{"path": "/x/y", "expanded": "/x/y", "nearest": None}]
    assert pg.should_short_circuit("check /x/y for problems", missing) is True
    assert pg.should_short_circuit("whats in /x/y", missing) is True
    # a write/create request must NOT be hijacked
    assert pg.should_short_circuit("create a file in /x/y", missing) is False
    assert pg.should_short_circuit("move the logs to /x/y", missing) is False
    # nothing missing -> never
    assert pg.should_short_circuit("check /x/y", []) is False


def test_honest_reply_offers_nearest():
    missing = [{"path": "/home/robofuse-stack", "expanded": "/home/robofuse-stack",
                "nearest": "/home/mickey/robofuse-stack"}]
    r = pg.honest_reply(missing)
    assert "can't find" in r.lower()
    assert "/home/mickey/robofuse-stack" in r
    assert "haven't inspected" in r.lower()


def test_short_circuit_broadened_write_verbs():
    missing = [{"path": "/tmp/newdir", "expanded": "/tmp/newdir", "nearest": None}]
    for msg in [
        "mkdir /tmp/newdir",
        "touch /tmp/newdir/a.txt",
        "create /tmp/newdir/app.py",
        "write /tmp/newdir/out.log",
        "save as /tmp/newdir/out.json",
        "export to /tmp/newdir/data.csv",
    ]:
        assert pg.should_short_circuit(msg, missing) is False, msg
