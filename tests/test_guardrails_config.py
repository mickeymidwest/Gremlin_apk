"""Tests for centralized config/guardrails.yaml, guardrails_config loader,
pregate immutable self-edit lock, two-tier search length gating, hard entity-overlap
abort, stale service-alias cleanup, and /tools/registry availability."""
import asyncio
import copy
import threading

import pytest

from magic_core import autoresearch, guardrails_config, pregate, service_control
from magic_core.server import create_app


def test_guardrails_loads_valid_repo_config():
    guardrails_config.reset_cache()
    cfg = guardrails_config.get_guardrails()
    assert "hard_gates" in cfg and "soft_prompt_rules" in cfg
    assert cfg["hard_gates"]["search"]["min_snippet_chars"] == 50
    assert cfg["hard_gates"]["search"]["min_total_evidence_chars"] == 200
    assert cfg["hard_gates"]["search"]["require_entity_overlap"] is True
    assert "config/guardrails.yaml" in cfg["hard_gates"]["pregate"]["protected_paths"]


def test_guardrails_validation_fails_clearly_on_missing_or_malformed_key():
    cfg = copy.deepcopy(guardrails_config.load_guardrails())
    del cfg["hard_gates"]["search"]["min_total_evidence_chars"]
    with pytest.raises(guardrails_config.GuardrailsConfigError, match="hard_gates\\.search\\.min_total_evidence_chars"):
        guardrails_config.validate_guardrails(cfg)

    cfg2 = copy.deepcopy(guardrails_config.load_guardrails())
    cfg2["hard_gates"]["search"]["min_snippet_chars"] = "not-an-int"
    with pytest.raises(guardrails_config.GuardrailsConfigError, match="wrong type"):
        guardrails_config.validate_guardrails(cfg2)

    cfg3 = copy.deepcopy(guardrails_config.load_guardrails())
    cfg3["hard_gates"]["search"]["trigger_patterns"]["tech_ask"] = "[unclosed"
    with pytest.raises(guardrails_config.GuardrailsConfigError, match="Invalid regex"):
        guardrails_config.validate_guardrails(cfg3)


def test_pregate_blocks_self_edit_on_guardrails_yaml_even_if_removed_from_yaml():
    assert "config/guardrails.yaml" in pregate._PROTECTED_PATHS
    assert "magic_core/guardrails_config.py" in pregate._PROTECTED_PATHS
    patch = (
        "diff --git a/config/guardrails.yaml b/config/guardrails.yaml\n"
        "--- a/config/guardrails.yaml\n"
        "+++ b/config/guardrails.yaml\n"
        "@@ -1,1 +1,1 @@\n"
        "-hard_gates:\n"
        "+hard_gates: {}\n"
    )
    fails = pregate._protected_path_edits(patch)
    assert any("config/guardrails.yaml" in f for f in fails)


def test_autoresearch_hard_aborts_on_zero_entity_overlap(monkeypatch):
    monkeypatch.setattr(
        autoresearch.web_search,
        "search",
        lambda q, limit=5, timeout=12: [
            {
                "title": "White Wolf (Hunter) - Marvel Database",
                "url": "https://marvel.fandom.com/wiki/Hunter_(White_Wolf)",
                "snippet": (
                    "Hunter is the adopted older brother of T'Challa and commander of the Hatut Zeraze secret police in Wakanda "
                    "featured in Marvel Comics storylines."
                ),
            }
        ],
    )
    monkeypatch.setattr(
        autoresearch.web_search,
        "fetch_text",
        lambda *a, **k: (
            "Hunter first appeared in Black Panther comic books published by Marvel Comics as leader of the Hatut Zeraze "
            "and estranged brother of King T'Challa."
        ),
    )
    srcs, ev = asyncio.run(autoresearch.gather("when was Isaac Newton knighted by Queen Anne?", force=True))
    assert srcs is None
    assert autoresearch.is_no_source(ev)


def test_autoresearch_length_gating_two_tier_drops_junk_and_fails_below_200(monkeypatch):
    # Individual junk snippet (<50) alongside a valid result (>=200 combined) keeps the valid one and drops the junk row
    monkeypatch.setattr(
        autoresearch.web_search,
        "search",
        lambda q, limit=5, timeout=12: [
            {
                "title": "Mount Fuji - Wikipedia",
                "url": "https://en.wikipedia.org/wiki/Mount_Fuji",
                "snippet": (
                    "Mount Fuji is an active stratovolcano located on Honshu Island in central Japan standing 3,776.24 meters tall above sea level, "
                    "making it the highest mountain peak in Japan and a UNESCO World Heritage Cultural Site designated since June 2013 southwest of Tokyo."
                ),
            },
            {
                "title": "Junk line",
                "url": "https://spam.example.com/fuji",
                "snippet": "too short",
            },
        ],
    )
    monkeypatch.setattr(autoresearch.web_search, "fetch_text", lambda *a, **k: "")
    srcs, ev = asyncio.run(autoresearch.gather("how tall is mount fuji", force=True))
    assert srcs and len(srcs) == 1
    assert "en.wikipedia.org" in srcs[0]["url"]
    assert "too short" not in ev

    # Combined evidence < min_total_evidence_chars (200) must fail closed even if snippet >= 50
    monkeypatch.setattr(
        autoresearch.web_search,
        "search",
        lambda q, limit=5, timeout=12: [
            {
                "title": "Mount Fuji",
                "url": "https://en.wikipedia.org/wiki/Mount_Fuji",
                "snippet": "Mount Fuji is located southwest of Tokyo in Japan and rises to 3,776 m above sea level.",
            }
        ],
    )
    srcs2, ev2 = asyncio.run(autoresearch.gather("how tall is mount fuji", force=True))
    assert srcs2 is None and autoresearch.is_no_source(ev2)


def test_service_control_dropped_stale_zoid_aliases_and_tools_registry_route(tmp_path, monkeypatch):
    (tmp_path / "config").mkdir()
    (tmp_path / "config" / "models.yaml").write_text(
        "service_control:\n  docker: [jellyfin]\n  systemd: [magic-update.timer]\n"
    )
    assert "battles" not in service_control._FRIENDLY
    assert service_control.resolve_unit(str(tmp_path), "updates") == ("systemd", "magic-update.timer")
    assert service_control.resolve_unit(str(tmp_path), "battles") is None

    monkeypatch.setattr(service_control, "live_docker_containers", lambda *a, **k: None)
    monkeypatch.setattr(service_control, "_systemctl_user_available", lambda: True)
    avail = service_control.available_service_tools(str(tmp_path))
    assert [e["name"] for e in avail] == ["magic-update.timer"]

    class _MinReg:
        backends = {"magic": object()}
        def names(self):
            return ["magic"]
        def primary_model_name(self):
            return "magic"
        def get(self, n):
            return self.backends.get(n)

    lp = asyncio.new_event_loop()
    th = threading.Thread(target=lp.run_forever, daemon=True)
    th.start()
    try:
        app = create_app(_MinReg(), router=None, project_root=tmp_path, loop=lp, token="tok", admin_token="adm")
        resp = app.test_client().get("/tools/registry", headers={"Authorization": "Bearer tok"})
        assert resp.status_code == 200
        body = resp.get_json()
        assert isinstance(body["tools"], list) and body["tools"]
        assert body["services"] == [{"name": "magic-update.timer", "kind": "systemd", "available": True}]
    finally:
        lp.call_soon_threadsafe(lp.stop)
        th.join(timeout=5)
