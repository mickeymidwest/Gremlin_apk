"""The file tools are reachable end-to-end: pre-filter lets the phrases through,
and actions.execute dispatches them to a real, jailed read."""
import asyncio

from magic_core import actions
from magic_core.intent import Intent, _ACTION_HINT


def _run(intent, root):
    return asyncio.run(actions.execute(intent, router=None, registry=None,
                                       project_root=root, history=""))


def test_read_file_end_to_end(tmp_path):
    (tmp_path / "a.txt").write_text("hello world\n")
    r = _run(Intent(action="read_file", args={"path": "a.txt"}, confidence=1.0), str(tmp_path))
    assert r["ok"] and "hello world" in r["answer"]


def test_search_files_end_to_end(tmp_path):
    (tmp_path / "x.py").write_text("PORT = 8765\n")
    r = _run(Intent(action="search_files", args={"pattern": "PORT ="}, confidence=1.0), str(tmp_path))
    assert r["ok"] and "x.py" in r["answer"]


def test_read_secret_refused_end_to_end(tmp_path):
    (tmp_path / ".sudo_credential").write_text("nope")
    r = _run(Intent(action="read_file", args={"path": ".sudo_credential"}, confidence=1.0), str(tmp_path))
    assert r["ok"] is False


def test_action_hint_matches_file_phrases():
    for phrase in ["read the file config/models.yaml",
                   "search the code for parse_port",
                   "grep for TODO",
                   "list the scripts folder",
                   "what's in config.yaml"]:
        assert _ACTION_HINT.search(phrase), phrase


def test_action_hint_ignores_plain_chat():
    # the file-op terms are specific enough not to fire on ordinary chat
    for phrase in ["tell me a joke", "how are you today", "what do you think about rust"]:
        assert not _ACTION_HINT.search(phrase), phrase
