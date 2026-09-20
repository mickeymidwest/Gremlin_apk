"""regression.py: persists a target's best-ever win across zoid_loop.py
runs (that loop's own `best` dict is fresh every process start, so
nothing previously caught a target that used to win and stopped)."""
from gremlin_core.magic import regression


def test_load_best_is_none_when_nothing_ever_won(tmp_path):
    assert regression.load_best(str(tmp_path), "convert") is None


def test_record_if_win_only_persists_a_real_win(tmp_path):
    regression.record_if_win(str(tmp_path), "convert", "convert", "b1", 0.6)
    assert regression.load_best(str(tmp_path), "convert") is None

    regression.record_if_win(str(tmp_path), "convert", "convert", "b2", 1.0)
    best = regression.load_best(str(tmp_path), "convert")
    assert best is not None
    assert best["score"] == 1.0
    assert best["battle_id"] == "b2"


def test_check_regression_none_when_no_prior_win_exists(tmp_path):
    msg = regression.check_regression(str(tmp_path), "convert", 0.3)
    assert msg is None


def test_check_regression_none_when_still_passing(tmp_path):
    regression.record_if_win(str(tmp_path), "convert", "convert", "b1", 1.0)
    msg = regression.check_regression(str(tmp_path), "convert", 1.0)
    assert msg is None


def test_check_regression_fires_loudly_after_a_real_prior_win(tmp_path):
    regression.record_if_win(str(tmp_path), "convert", "convert", "b1", 1.0)
    msg = regression.check_regression(str(tmp_path), "convert", 0.4)
    assert msg is not None
    assert "REGRESSION" in msg
    assert "convert" in msg
    assert "0.40" in msg


def test_record_if_win_overwrites_with_the_freshest_win(tmp_path):
    regression.record_if_win(str(tmp_path), "convert", "convert", "b1", 1.0)
    regression.record_if_win(str(tmp_path), "convert", "convert", "b2", 1.0)
    best = regression.load_best(str(tmp_path), "convert")
    assert best["battle_id"] == "b2"


def test_check_regression_then_record_is_the_correct_call_order(tmp_path):
    # a caller checks BEFORE recording, so a bad round's own low score
    # never overwrites the win evidence needed to flag it as a regression
    regression.record_if_win(str(tmp_path), "convert", "convert", "b1", 1.0)
    msg = regression.check_regression(str(tmp_path), "convert", 0.2)
    assert msg is not None
    regression.record_if_win(str(tmp_path), "convert", "convert", "b2", 0.2)  # not a win, no-op
    still_there = regression.load_best(str(tmp_path), "convert")
    assert still_there["battle_id"] == "b1"  # untouched


def test_different_targets_are_tracked_independently(tmp_path):
    regression.record_if_win(str(tmp_path), "convert", "convert", "b1", 1.0)
    assert regression.load_best(str(tmp_path), "listops") is None
    assert regression.load_best(str(tmp_path), "convert") is not None


def test_load_best_survives_a_corrupt_file_without_crashing(tmp_path):
    (tmp_path / "data" / "magic" / "regression").mkdir(parents=True)
    (tmp_path / "data" / "magic" / "regression" / "convert.json").write_text("{not json")
    assert regression.load_best(str(tmp_path), "convert") is None
