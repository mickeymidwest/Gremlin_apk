"""N15: mutation_log scrubs secrets before writing."""
from gremlin_core import mutation_log as M


def test_scrub_redacts_known_secret_shapes():
    s = M._scrub_str(
        "export GITHUB_TOKEN=ghp_abcdefghijklmnopqrstuvwxyz0123456789 and "
        "AWS AKIAIOSFODNN7EXAMPLE and key sk-abcdefghijklmnopqrstuvwx and "
        "google AIzaSyDaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa and "
        "node 100.64.0.1")
    assert "ghp_" not in s and "AKIAIOSFODNN7EXAMPLE" not in s
    assert "sk-abcdefghijklmnopqrstuvwx" not in s
    assert "AIzaSyD" not in s
    assert "100.64.0.1" not in s
    assert s.count("[REDACTED]") >= 5


def test_scrub_kv_literals_but_not_code():
    assert "[REDACTED]" in M._scrub_str('password = "s3cr3t_hunter2_value"')
    assert "[REDACTED]" in M._scrub_str("api_key: AbCdEf0123456789XyZ")
    # a short/computed value is not a secret literal -> left alone
    assert M._scrub_str("token = get_token()") == "token = get_token()"
    assert M._scrub_str("password=input()") == "password=input()"


def test_scrub_is_recursive():
    out = M._scrub({"a": {"b": ["ghp_abcdefghijklmnopqrstuvwxyz0123456789"]}, "n": 5})
    assert out["a"]["b"][0] == "[REDACTED]"
    assert out["n"] == 5          # non-strings untouched


def test_append_mutation_scrubs_on_disk(tmp_path):
    M.append_mutation(str(tmp_path), {
        "kind": "self_edit",
        "prompt": "set the token to ghp_abcdefghijklmnopqrstuvwxyz0123456789 please",
        "ok": True})
    entries = M.read_mutations(str(tmp_path))
    assert entries and "ghp_" not in entries[0]["prompt"]
    assert "[REDACTED]" in entries[0]["prompt"]
    assert entries[0]["ok"] is True and entries[0]["kind"] == "self_edit"


def test_benign_entry_unchanged(tmp_path):
    M.append_mutation(str(tmp_path), {"kind": "fix", "path": "app.py", "ok": True})
    e = M.read_mutations(str(tmp_path))[0]
    assert e["path"] == "app.py" and "[REDACTED]" not in str(e)
