"""N9 battle-preflight: disk gate in the zoid loop."""
import zoid_loop as Z


def test_disk_free_gb_is_positive_on_real_fs():
    assert Z._disk_free_gb("/") > 0


def test_disk_gate_skips_when_low(monkeypatch):
    logs = []
    monkeypatch.setattr(Z, "_disk_free_gb", lambda path="/": 1.0)
    assert Z._disk_gate(logs.append, floor_gb=3.0) is False
    assert any("skipping" in l for l in logs)


def test_disk_gate_proceeds_when_ample(monkeypatch):
    monkeypatch.setattr(Z, "_disk_free_gb", lambda path="/": 100.0)
    assert Z._disk_gate(lambda m: None, floor_gb=3.0) is True


def test_unreadable_path_never_stalls(monkeypatch):
    # an OSError in disk_usage -> big number -> loop proceeds
    import shutil
    monkeypatch.setattr(shutil, "disk_usage", lambda p: (_ for _ in ()).throw(OSError()))
    assert Z._disk_free_gb("/nope") == 999.0
