"""Recipes: discover, validate, render parameterized workflows."""
from magic_core import recipes


def _mk(tmp_path, name, body):
    d = tmp_path / "data" / "recipes"
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{name}.yaml").write_text(body)


GOOD = """\
name: audit-service
description: Security sweep of a service
mode: plan
parameters:
  - name: service
    required: true
  - name: depth
    default: quick
prompt: |
  Audit the {service} service ({depth} pass): unit, ports, permissions.
"""


def test_list_and_load(tmp_path):
    _mk(tmp_path, "audit-service", GOOD)
    rows = recipes.list_recipes(str(tmp_path))
    assert len(rows) == 1 and rows[0]["name"] == "audit-service"
    assert rows[0]["ok"] and rows[0]["mode"] == "plan"
    r = recipes.load_recipe(str(tmp_path), "audit-service")
    assert r and r["description"].startswith("Security")


def test_render_fills_params_and_defaults(tmp_path):
    _mk(tmp_path, "audit-service", GOOD)
    r = recipes.load_recipe(str(tmp_path), "audit-service")
    out = recipes.render(r, {"service": "jellyfin"})
    assert out["ok"] and out["mode"] == "plan"
    assert "jellyfin" in out["prompt"] and "quick" in out["prompt"]  # default used


def test_render_reports_missing_required(tmp_path):
    _mk(tmp_path, "audit-service", GOOD)
    r = recipes.load_recipe(str(tmp_path), "audit-service")
    out = recipes.render(r, {})           # no service
    assert not out["ok"] and out["missing"] == ["service"]


def test_validate_catches_bad_mode_and_undeclared_placeholder(tmp_path):
    bad = "name: x\nmode: turbo\nprompt: hit {undeclared} now\n"
    r = {"name": "x", "mode": "turbo", "prompt": "hit {undeclared} now", "parameters": []}
    errs = recipes.validate(r)
    assert any("mode must be" in e for e in errs)
    assert any("undeclared" in e for e in errs)


def test_parse_args():
    assert recipes.parse_args(["service=jellyfin", "depth=deep", "junk"]) == {
        "service": "jellyfin", "depth": "deep"}


def test_missing_dir_is_empty(tmp_path):
    assert recipes.list_recipes(str(tmp_path)) == []
