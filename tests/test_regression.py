"""R7 bench-regression comparator."""
import json

import importlib.util
from pathlib import Path

# bench/ isn't a package; load regression.py by path
_SPEC = importlib.util.spec_from_file_location(
    "bench_regression", str(Path(__file__).resolve().parents[1] / "bench" / "regression.py"))
R = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(R)


def _run(model, suite, **parts):
    return {"model": model, "suite": suite,
            "parts": {k: {"score": v[0], "total": v[1]} for k, v in parts.items()}}


def test_no_regression_on_single_run():
    runs = [_run("gremlin", "h1", quality=(19, 19))]
    assert R.regressions(runs) == []


def test_flags_a_drop_vs_best_prior():
    runs = [
        _run("gremlin", "h1", quality=(19, 19), honesty=(9, 12)),
        _run("gremlin", "h1", quality=(19, 19), honesty=(11, 12)),   # best honesty 11
        _run("gremlin", "h1", quality=(16, 19), honesty=(11, 12)),   # quality dropped 19->16
    ]
    out = R.regressions(runs, min_drop=2)
    joined = " ".join(out)
    assert "quality dropped to 16/19" in joined and "best was 19" in joined
    assert "honesty" not in joined           # honesty held at its best


def test_small_dip_within_tolerance_is_ignored():
    runs = [
        _run("gremlin", "h1", quality=(19, 19)),
        _run("gremlin", "h1", quality=(18, 19)),   # -1, under min_drop=2
    ]
    assert R.regressions(runs, min_drop=2) == []


def test_different_suite_is_not_compared():
    runs = [
        _run("gremlin", "h1", quality=(19, 19)),
        _run("gremlin", "h2", quality=(5, 19)),    # different suite -> not a regression
    ]
    assert R.regressions(runs) == []


def test_alert_file_roundtrip(tmp_path, monkeypatch):
    monkeypatch.setattr(R, "ALERT", str(tmp_path / "bench-regression.txt"))
    R.write_alert(["gremlin: quality dropped to 16/19 (best was 19) -- possible regression"])
    assert R.current_alert() == ["gremlin: quality dropped to 16/19 (best was 19) -- possible regression"]
    R.write_alert([])                      # clean run removes the file
    assert R.current_alert() == []


def test_load_runs_reads_jsonl(tmp_path):
    p = tmp_path / "quality-2026-11-09.jsonl"
    p.write_text(json.dumps(_run("gremlin", "h1", quality=(19, 19))) + "\n\n"
                 + json.dumps(_run("gremlin", "h1", quality=(17, 19))) + "\n")
    runs = R.load_runs([str(p)])
    assert len(runs) == 2 and runs[0]["parts"]["quality"]["score"] == 19
