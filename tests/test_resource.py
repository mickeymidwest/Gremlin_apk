"""Unified resource governor (Organoid #4)."""
from gremlin_core import resource as R


def test_real_readings_are_sane():
    # on the real box these read; the type/sign is what we assert
    d = R.disk_free_gb("/")
    assert d is None or d > 0
    hr = R.headroom()
    assert set(hr) == {"ram_gb", "disk_gb", "vram_free_mb"}


def test_ok_for_heavy_blocks_on_low_ram(monkeypatch):
    monkeypatch.setattr(R, "ram_available_gb", lambda: 0.3)
    monkeypatch.setattr(R, "disk_free_gb", lambda path="/": 500.0)
    ok, reason = R.ok_for_heavy()
    assert ok is False and "low RAM" in reason


def test_ok_for_heavy_blocks_on_low_disk(monkeypatch):
    monkeypatch.setattr(R, "ram_available_gb", lambda: 5.0)
    monkeypatch.setattr(R, "disk_free_gb", lambda path="/": 1.0)
    ok, reason = R.ok_for_heavy()
    assert ok is False and "low disk" in reason


def test_ok_for_heavy_passes_with_headroom(monkeypatch):
    monkeypatch.setattr(R, "ram_available_gb", lambda: 5.0)
    monkeypatch.setattr(R, "disk_free_gb", lambda path="/": 500.0)
    assert R.ok_for_heavy() == (True, "ok")


def test_unreadable_probes_never_block(monkeypatch):
    # a missing reading (None) must not block a heavy job
    monkeypatch.setattr(R, "ram_available_gb", lambda: None)
    monkeypatch.setattr(R, "disk_free_gb", lambda path="/": None)
    assert R.ok_for_heavy()[0] is True
