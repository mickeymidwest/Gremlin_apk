"""R7 self-check: flags a part that regressed between the two newest runs."""
import json

from gremlin_core import selfcheck


def _write(d, *runs):
    with open(d / "quality-2026-11-08.jsonl", "w") as f:
        for r in runs:
            f.write(json.dumps(r) + "\n")


def _run(name, suite, ts, **parts):
    return {"name": name, "suite": suite, "ts": ts,
            "parts": {k: {"score": v, "total": 19} for k, v in parts.items()}}


def test_no_data(tmp_path):
    reg, msg = selfcheck.compare(str(tmp_path))
    assert not reg and "No benchmark runs" in msg


def test_detects_regression(tmp_path):
    _write(tmp_path,
           _run("qwen3.5-9b", "s1", "2026-11-07T10:00", quality=19, honesty=10),
           _run("qwen3.5-9b", "s1", "2026-11-08T10:00", quality=19, honesty=7))  # honesty dropped 3
    reg, msg = selfcheck.compare(str(tmp_path))
    assert reg and "REGRESSION" in msg and "honesty 10->7" in msg


def test_small_drop_within_tolerance_is_ok(tmp_path):
    _write(tmp_path,
           _run("m", "s1", "2026-11-07T10:00", quality=19),
           _run("m", "s1", "2026-11-08T10:00", quality=18))  # -1, within tolerance
    reg, msg = selfcheck.compare(str(tmp_path))
    assert not reg and "no drop" in msg


def test_only_compares_same_suite(tmp_path):
    _write(tmp_path,
           _run("m", "s1", "2026-11-07T10:00", quality=19),
           _run("m", "s2", "2026-11-08T10:00", quality=5))  # different suite, not comparable
    reg, msg = selfcheck.compare(str(tmp_path))
    assert not reg


def test_filters_by_model(tmp_path):
    _write(tmp_path,
           _run("a", "s1", "2026-11-07T10:00", quality=19),
           _run("a", "s1", "2026-11-08T10:00", quality=10),
           _run("b", "s1", "2026-11-07T10:00", quality=19),
           _run("b", "s1", "2026-11-08T10:00", quality=19))
    reg, _ = selfcheck.compare(str(tmp_path), model="b")
    assert not reg
    reg, _ = selfcheck.compare(str(tmp_path), model="a")
    assert reg
