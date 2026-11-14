"""Preventive error ledger: record failures, consult before repeating."""
from magic_core import error_ledger as EL


def test_record_then_recall_matches_normalized_context(tmp_path):
    root = str(tmp_path)
    # a service restart that hit a permission wall
    e = EL.record(root, "restart magic.service (pid 1234)", "bash: permission denied")
    assert e is not None and e["count"] == 1
    # the SAME step next week (different pid) collapses to one signature
    hit = EL.recall(root, "restart magic.service (pid 9999)")
    assert hit is not None and "permission" in hit["what"]


def test_repeat_failure_bumps_count(tmp_path):
    root = str(tmp_path)
    EL.record(root, "systemctl restart x.timer", "Failed to restart x.timer: Unit not found")
    EL.record(root, "systemctl restart x.timer", "Failed to restart x.timer: Unit not found")
    e = EL.record(root, "systemctl restart x.timer", "Failed to restart x.timer: Unit not found")
    assert e["count"] == 3


def test_unrecognized_error_is_not_ledgered(tmp_path):
    root = str(tmp_path)
    # self_correct.diagnose returns None -> nothing recorded, nothing recalled
    assert EL.record(root, "do a thing", "everything went perfectly fine") is None
    assert EL.recall(root, "do a thing") is None


def test_preflight_note_is_preventive_and_empty_when_unseen(tmp_path):
    root = str(tmp_path)
    assert EL.preflight_note(root, "never seen this") == ""
    EL.record(root, "mount /data", "write error: No space left on device")
    note = EL.preflight_note(root, "mount /data")
    assert "failed before" in note and "disk is full" in note


def test_secrets_are_scrubbed_before_persist(tmp_path):
    root = str(tmp_path)
    EL.record(root, "curl api with token=sk-supersecretvalue123456",
              "connection refused to api")
    raw = (tmp_path / "data" / "error_ledger.json").read_text()
    assert "supersecretvalue" not in raw
    assert "REDACTED" in raw


def test_recall_prefers_most_repeated(tmp_path):
    root = str(tmp_path)
    # two different failure classes on the same step fingerprint
    EL.record(root, "start jellyfin", "permission denied")
    for _ in range(3):
        EL.record(root, "start jellyfin", "No such container: jellyfin")
    hit = EL.recall(root, "start jellyfin")
    assert "container" in hit["what"]        # the 3x one wins over the 1x one


def test_eviction_keeps_under_cap(tmp_path, monkeypatch):
    root = str(tmp_path)
    monkeypatch.setattr(EL, "_MAX_ENTRIES", 3)
    # distinct NON-digit contexts -> distinct fingerprints -> distinct entries
    for word in ["alpha", "bravo", "charlie", "delta", "echo", "foxtrot"]:
        EL.record(root, f"run the {word} task", "zsh: command not found: foo")
    data = EL._load(root)
    assert len(data) <= 3
