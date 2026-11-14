"""override-aware resolve_unit (the last-mile service control)."""
from magic_core import service_control as sc


def test_allow_any_resolves_user_units(tmp_path):
    root = str(tmp_path)
    # off the allow-list: refused normally...
    assert sc.resolve_unit(root, "magic-zoid.timer") is None
    # ...but allowed when override (allow_any) is on
    assert sc.resolve_unit(root, "magic-zoid.timer", allow_any=True) == ("systemd", "magic-zoid.timer")
    assert sc.resolve_unit(root, "magic-update", allow_any=True) == ("systemd", "magic-update.service")
    assert sc.resolve_unit(root, "sonarr", allow_any=True) == ("docker", "sonarr")


def test_allow_any_still_rejects_unsafe_names(tmp_path):
    root = str(tmp_path)
    for bad in ("foo; rm -rf /", "a && b", "$(whoami)", "x`id`", "two words"):
        assert sc.resolve_unit(root, bad, allow_any=True) is None


def test_allowlist_still_wins_without_override(tmp_path):
    root = str(tmp_path)
    sc.add_allow(root, "docker", "jellyfin")
    assert sc.resolve_unit(root, "jellyfin") == ("docker", "jellyfin")   # no override needed
    sc.remove_allow(root, "jellyfin")
