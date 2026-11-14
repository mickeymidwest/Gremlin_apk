"""check_file reachable end-to-end + the pre-filter lets it through."""
import asyncio

from magic_core import actions
from magic_core.intent import Intent, _ACTION_HINT


def _run(intent, root):
    return asyncio.run(actions.execute(intent, router=None, registry=None,
                                       project_root=root, history=""))


def test_check_file_flags_broken_python(tmp_path):
    (tmp_path / "bad.py").write_text("def f(:\n    pass\n")
    r = _run(Intent(action="check_file", args={"path": "bad.py"}, confidence=1.0), str(tmp_path))
    assert r["ok"] is False and "line 1" in r["answer"]


def test_check_file_clean(tmp_path):
    (tmp_path / "ok.py").write_text("def f():\n    return 1\n")
    r = _run(Intent(action="check_file", args={"path": "ok.py"}, confidence=1.0), str(tmp_path))
    assert r["ok"] and "no problems" in r["answer"]


def test_check_file_secret_refused(tmp_path):
    (tmp_path / ".sudo_credential").write_text("x")
    r = _run(Intent(action="check_file", args={"path": ".sudo_credential"}, confidence=1.0), str(tmp_path))
    assert r["ok"] is False


def test_hint_matches_check_phrases():
    for p in ["does auth.py compile", "lint the config", "check server.py",
              "diagnose this file"]:
        assert _ACTION_HINT.search(p), p
