"""service_control: the can-do allow-list add/remove via the extra file."""
from magic_core import service_control as sc


def test_add_makes_a_service_resolvable(tmp_path):
    root = str(tmp_path)
    assert "magic-zoid.timer" not in sc.allowed_names(root)
    assert sc.resolve_unit(root, "magic-zoid.timer") is None
    sc.add_allow(root, "systemd", "magic-zoid.timer")
    assert "magic-zoid.timer" in sc.allowed_names(root)
    assert sc.resolve_unit(root, "magic-zoid.timer") == ("systemd", "magic-zoid.timer")


def test_add_docker_and_remove(tmp_path):
    root = str(tmp_path)
    sc.add_allow(root, "docker", "sonarr")
    assert sc.resolve_unit(root, "sonarr") == ("docker", "sonarr")
    sc.remove_allow(root, "sonarr")
    assert sc.resolve_unit(root, "sonarr") is None


def test_models_yaml_is_not_rewritten(tmp_path):
    # the config file is never touched; additions live in data/service_allow.json
    (tmp_path / "config").mkdir()
    cfg = tmp_path / "config" / "models.yaml"
    cfg.write_text("# my comment\nservice_control:\n  docker: [jellyfin]\n")
    sc.add_allow(str(tmp_path), "systemd", "magic-update.timer")
    assert "# my comment" in cfg.read_text()          # untouched
    assert (tmp_path / "data" / "service_allow.json").is_file()
    names = sc.allowed_names(str(tmp_path))
    assert "jellyfin" in names and "magic-update.timer" in names   # merged


def test_add_defaults_bad_kind_to_systemd(tmp_path):
    sc.add_allow(str(tmp_path), "nonsense", "foo.service")
    assert sc.resolve_unit(str(tmp_path), "foo.service") == ("systemd", "foo.service")
