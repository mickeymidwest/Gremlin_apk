"""N12 weekly self-audit: the pure compose() report logic + run() smoke."""
from gremlin_core import self_audit as A


def test_compose_all_clear():
    out = A.compose(security=[("info", "no firewall")], drift=[],
                    checkpoint_age_days=0.5, disk_free=500.0, mem_avail=5.0)
    assert "All clear" in out
    assert "none" in out and "fresh" in out


def test_compose_flags_high_and_drift_and_stale():
    out = A.compose(
        security=[("high", "SSH allows password logins"), ("medium", "no firewall")],
        drift=["doc X references missing file"],
        checkpoint_age_days=9.0, disk_free=8.0, mem_avail=0.2)
    assert "Worth a look" in out
    assert "[HIGH] SSH allows password logins" in out
    assert "1 contradiction" in out
    assert "STALE" in out
    assert "disk 8 GB free — LOW" in out
    assert "RAM 0.2 GB available — LOW" in out


def test_compose_handles_no_checkpoint():
    out = A.compose(security=[], drift=[], checkpoint_age_days=None,
                    disk_free=500.0, mem_avail=5.0)
    assert "NONE yet" in out and "Worth a look" in out


def test_compose_handles_missing_headroom_probes():
    out = A.compose(security=[], drift=[], checkpoint_age_days=1.0,
                    disk_free=None, mem_avail=None)
    assert "Headroom" not in out and "All clear" in out


def test_run_smoke(tmp_path):
    # run() must never raise even with no git repo / guard unavailable
    out = A.run(str(tmp_path))
    assert "weekly self-audit" in out
    assert out.endswith("All clear.") or out.endswith("Worth a look.")
