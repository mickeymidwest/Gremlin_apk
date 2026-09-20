"""Roadmap #107: service_status/service_restart, scoped to the
service_control: allow-list in config/models.yaml. Off by default --
a name not listed is refused structurally (resolve_unit returns None),
the same way run_command's robofuse guard is structural rather than
prompt-text-only. Never touches a real docker/systemctl process in
these tests -- get_status/restart are monkeypatched at the module
boundary, resolve_unit is pure config parsing."""
import asyncio

import gremlin_core.service_control as service_control
from gremlin_core import tools as tools_mod


def _project_root_with(tmp_path, yaml_text):
    (tmp_path / "config").mkdir()
    (tmp_path / "config" / "models.yaml").write_text(yaml_text)
    return str(tmp_path)


_YAML = """
service_control:
  docker:
    - jellyfin
    - jellyseerr
  systemd:
    - gremlin.service
"""


def test_allow_listed_docker_name_resolves(tmp_path):
    root = _project_root_with(tmp_path, _YAML)
    assert service_control.resolve_unit(root, "jellyfin") == ("docker", "jellyfin")


def test_allow_listed_systemd_name_resolves_with_or_without_suffix(tmp_path):
    root = _project_root_with(tmp_path, _YAML)
    assert service_control.resolve_unit(root, "gremlin.service") == ("systemd", "gremlin.service")
    assert service_control.resolve_unit(root, "gremlin") == ("systemd", "gremlin.service")


def test_robofuse_containers_are_never_on_the_default_allow_list(tmp_path):
    # the shipped config/models.yaml deliberately never lists these --
    # this test would fail the moment someone adds one, which is exactly
    # the point: it has to be a deliberate edit, not an accident.
    root = _project_root_with(tmp_path, _YAML)
    for name in ("robofuse", "bridge", "unarr"):
        assert service_control.resolve_unit(root, name) is None


def test_name_not_on_list_refuses(tmp_path):
    root = _project_root_with(tmp_path, _YAML)
    assert service_control.resolve_unit(root, "some-random-thing") is None


def test_empty_config_allows_nothing(tmp_path):
    root = _project_root_with(tmp_path, "service_control:\n  docker: []\n  systemd: []\n")
    assert service_control.resolve_unit(root, "jellyfin") is None


def test_missing_config_file_allows_nothing(tmp_path):
    (tmp_path / "config").mkdir()
    assert service_control.resolve_unit(str(tmp_path), "jellyfin") is None


def test_service_restart_tool_refuses_a_name_not_on_the_list(tmp_path, monkeypatch):
    root = _project_root_with(tmp_path, _YAML)
    called = []
    monkeypatch.setattr(service_control, "restart", lambda k, n: called.append((k, n)))

    ctx = tools_mod.ExecContext(router=None, registry=None, project_root=root)
    result = asyncio.run(tools_mod._tool_service_restart({"name": "robofuse"}, ctx))
    assert result["ok"] is False
    assert "allow-list" in result["answer"]
    assert not called


def test_service_restart_tool_calls_through_for_an_allowed_name(tmp_path, monkeypatch):
    from gremlin_core.sandbox import SandboxResult

    root = _project_root_with(tmp_path, _YAML)
    calls = []

    async def _fake_restart(kind, name):
        calls.append((kind, name))
        return SandboxResult(stdout="restarted", stderr="", exit_code=0, timed_out=False)

    monkeypatch.setattr(service_control, "restart", _fake_restart)

    ctx = tools_mod.ExecContext(router=None, registry=None, project_root=root)
    result = asyncio.run(tools_mod._tool_service_restart({"name": "jellyfin"}, ctx))
    assert result["ok"] is True
    assert calls == [("docker", "jellyfin")]


def test_service_status_tool_is_read_only_and_never_mutates():
    assert tools_mod.REGISTRY.get("service_status").capability == "read_only"
    assert tools_mod.REGISTRY.get("service_status").destructive is False


def test_service_restart_tool_is_mutates_external_and_destructive():
    tool = tools_mod.REGISTRY.get("service_restart")
    assert tool.capability == "mutates_external"
    assert tool.destructive is True
