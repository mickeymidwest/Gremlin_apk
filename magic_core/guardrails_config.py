"""Shared loader and startup validator for config/guardrails.yaml."""
from __future__ import annotations

import functools
import re
from pathlib import Path
from typing import Any

import yaml

DEFAULT_CONFIG_PATH = Path(__file__).resolve().parent.parent / "config" / "guardrails.yaml"


class GuardrailsConfigError(ValueError):
    """Raised when config/guardrails.yaml is missing, unreadable, or malformed."""


def _require_mapping(parent: Any, key: str, dotted: str) -> dict[str, Any]:
    if not isinstance(parent, dict) or key not in parent:
        raise GuardrailsConfigError(f"Missing required guardrails section '{dotted}'")
    val = parent[key]
    if not isinstance(val, dict):
        raise GuardrailsConfigError(f"Guardrails section '{dotted}' must be a mapping")
    return val


def _require_key(mapping: dict[str, Any], key: str, dotted: str, expected_type: type | tuple[type, ...]) -> Any:
    if key not in mapping:
        raise GuardrailsConfigError(f"Missing required guardrails key '{dotted}'")
    val = mapping[key]
    if expected_type is (int, float) and isinstance(val, bool):
        raise GuardrailsConfigError(f"Guardrails key '{dotted}' must be a number, got bool")
    if expected_type is int and isinstance(val, bool):
        raise GuardrailsConfigError(f"Guardrails key '{dotted}' must be an int, got bool")
    if not isinstance(val, expected_type):
        raise GuardrailsConfigError(
            f"Guardrails key '{dotted}' has wrong type {type(val).__name__}"
        )
    if expected_type is str and not val.strip():
        raise GuardrailsConfigError(f"Guardrails key '{dotted}' must not be empty")
    return val


def _validate_regex(pattern: str, dotted: str) -> None:
    try:
        re.compile(pattern, re.I)
    except re.error as exc:
        raise GuardrailsConfigError(f"Invalid regex at '{dotted}': {exc}") from exc


def validate_guardrails(data: Any) -> dict[str, Any]:
    """Validate structure, required keys, and regex syntax of guardrails config."""
    if not isinstance(data, dict):
        raise GuardrailsConfigError("config/guardrails.yaml root must be a YAML mapping")

    hard = _require_mapping(data, "hard_gates", "hard_gates")
    soft = _require_mapping(data, "soft_prompt_rules", "soft_prompt_rules")

    # hard_gates.pregate
    pg = _require_mapping(hard, "pregate", "hard_gates.pregate")
    _require_key(pg, "min_test_ram_mb", "hard_gates.pregate.min_test_ram_mb", int)
    _require_key(pg, "test_timeout_s", "hard_gates.pregate.test_timeout_s", int)
    for k in (
        "protected_paths",
        "protected_prefixes",
        "protected_tokens",
        "protected_call_names",
        "banned_decision_modules",
        "banned_finetune_packages",
        "banned_finetune_symbols",
    ):
        _require_key(pg, k, f"hard_gates.pregate.{k}", list)

    # hard_gates.search
    sr = _require_mapping(hard, "search", "hard_gates.search")
    for k in (
        "min_snippet_chars",
        "min_total_evidence_chars",
        "fetch_snippet_threshold",
        "max_fetch_pages",
        "max_page_chars",
        "min_fetched_page_chars",
        "min_clean_page_chars",
        "gather_limit",
        "gather_keep",
        "footer_domain_limit",
        "max_cited_figs",
        "max_query_tokens",
        "max_condense_tokens",
        "min_draft_fact_check_chars",
        "min_fallback_search_chars",
        "blocked_evidence_scan_chars",
        "blocked_evidence_max_doc_chars",
    ):
        v = _require_key(sr, k, f"hard_gates.search.{k}", int)
        if v < 0:
            raise GuardrailsConfigError(f"Guardrails key 'hard_gates.search.{k}' must be >= 0")
    _require_key(sr, "require_entity_overlap", "hard_gates.search.require_entity_overlap", bool)
    for k in ("trusted_hosts", "academic_hosts", "clickbait_hosts", "social_hosts", "spam_ai_hosts"):
        _require_key(sr, k, f"hard_gates.search.{k}", list)
    for k in ("forum_host_regex", "forum_path_regex", "ai_dir_host_regex", "blocked_evidence_regex"):
        _validate_regex(_require_key(sr, k, f"hard_gates.search.{k}", str), f"hard_gates.search.{k}")
    veto = _require_mapping(sr, "veto_patterns", "hard_gates.search.veto_patterns")
    for k in ("self_local", "creative", "opinion", "smalltalk"):
        _validate_regex(_require_key(veto, k, f"hard_gates.search.veto_patterns.{k}", str), f"hard_gates.search.veto_patterns.{k}")
    trig = _require_mapping(sr, "trigger_patterns", "hard_gates.search.trigger_patterns")
    for k in (
        "time_sensitive",
        "factual_lookup",
        "declarative_lookup",
        "cli_tool",
        "tech_ask",
        "wants_page",
        "event_lookup",
        "correction_cues",
        "draft_fact_claims",
    ):
        _validate_regex(_require_key(trig, k, f"hard_gates.search.trigger_patterns.{k}", str), f"hard_gates.search.trigger_patterns.{k}")

    # hard_gates.casual_fast_path
    cfp = _require_mapping(hard, "casual_fast_path", "hard_gates.casual_fast_path")
    _require_key(cfp, "max_words", "hard_gates.casual_fast_path.max_words", int)
    _require_key(cfp, "allow_question_mark", "hard_gates.casual_fast_path.allow_question_mark", bool)
    for k in ("greetings_and_reactions_regex", "fact_cue_regex"):
        _validate_regex(_require_key(cfp, k, f"hard_gates.casual_fast_path.{k}", str), f"hard_gates.casual_fast_path.{k}")

    # hard_gates.reply
    rp = _require_mapping(hard, "reply", "hard_gates.reply")
    for k in ("grounding_check", "revise_default", "plan_default", "chat_agent_default"):
        _require_key(rp, k, f"hard_gates.reply.{k}", bool)
    for k in (
        "hard_prompt_min_words",
        "worth_reviewing_min_words",
        "max_tokens_think",
        "max_tokens_default",
        "private_max_tokens",
        "stream_secret_tail_holdback",
        "memory_max_chars",
        "skills_limit",
        "skills_full_limit",
        "skills_procedure_steps",
        "repeat_nudge_chars",
        "subagent_max_steps",
    ):
        _require_key(rp, k, f"hard_gates.reply.{k}", int)
    for k in ("temperature", "private_temperature", "subagent_time_budget_s"):
        _require_key(rp, k, f"hard_gates.reply.{k}", (int, float))
    rc = _require_mapping(rp, "retry_caps", "hard_gates.reply.retry_caps")
    for k in ("grounding", "action_honesty", "code_check", "web_fact_check"):
        _require_key(rc, k, f"hard_gates.reply.retry_caps.{k}", int)
    for k in ("hard_keywords_regex", "think_pair_regex", "think_close_regex", "admits_gap_regex", "unverified_admit_regex"):
        _validate_regex(_require_key(rp, k, f"hard_gates.reply.{k}", str), f"hard_gates.reply.{k}")

    # hard_gates.grounding
    gr = _require_mapping(hard, "grounding", "hard_gates.grounding")
    _require_key(gr, "live_mark", "hard_gates.grounding.live_mark", str)
    _require_key(gr, "skip_prefixes", "hard_gates.grounding.skip_prefixes", list)
    _require_key(gr, "skip_exact", "hard_gates.grounding.skip_exact", list)
    _require_key(gr, "max_path_slashes", "hard_gates.grounding.max_path_slashes", int)
    _require_key(gr, "max_unsupported_web", "hard_gates.grounding.max_unsupported_web", int)

    # hard_gates.action_honesty
    ah = _require_mapping(hard, "action_honesty", "hard_gates.action_honesty")
    rules = _require_key(ah, "tool_output_rules", "hard_gates.action_honesty.tool_output_rules", list)
    for i, item in enumerate(rules):
        if not isinstance(item, dict) or not isinstance(item.get("name"), str) or not isinstance(item.get("pattern"), str):
            raise GuardrailsConfigError(f"Invalid item at 'hard_gates.action_honesty.tool_output_rules[{i}]'")
        _validate_regex(item["pattern"], f"hard_gates.action_honesty.tool_output_rules[{i}].pattern")
    for k in (
        "backtick_cmd_regex",
        "ran_claim_regex",
        "receipt_tag_regex",
        "user_sourced_regex",
        "meta_frame_regex",
        "scan_narration_regex",
        "results_report_regex",
        "web_search_claim_regex",
    ):
        _validate_regex(_require_key(ah, k, f"hard_gates.action_honesty.{k}", str), f"hard_gates.action_honesty.{k}")

    # hard_gates.service_control
    sc = _require_mapping(hard, "service_control", "hard_gates.service_control")
    _require_key(sc, "allowed_docker", "hard_gates.service_control.allowed_docker", list)
    _require_key(sc, "allowed_systemd", "hard_gates.service_control.allowed_systemd", list)
    _require_mapping(sc, "friendly_aliases", "hard_gates.service_control.friendly_aliases")
    for k in ("status_timeout_s", "action_timeout_s"):
        _require_key(sc, k, f"hard_gates.service_control.{k}", int)
    for k in ("docker_ps_timeout_s", "docker_cache_ttl_s", "restart_settle_s", "health_timeout_s"):
        _require_key(sc, k, f"hard_gates.service_control.{k}", (int, float))
    _require_key(sc, "health_url", "hard_gates.service_control.health_url", str)
    _require_key(sc, "health_target_unit", "hard_gates.service_control.health_target_unit", str)
    for k in ("status_cue_regex", "not_status_question_regex", "asking_regex", "group_regex"):
        _validate_regex(_require_key(sc, k, f"hard_gates.service_control.{k}", str), f"hard_gates.service_control.{k}")

    # soft_prompt_rules
    for k in (
        "no_usable_source_instruction",
        "no_source_reply",
        "web_evidence_instructions",
        "no_web_or_tools_guard_prompt",
        "grounding_retry_prompt",
        "code_check_retry_prompt",
        "grounding_caveat_prefix",
        "web_caveat_template",
        "action_honesty_caveat",
    ):
        _require_key(soft, k, f"soft_prompt_rules.{k}", str)

    return data


def load_guardrails(path: str | Path | None = None) -> dict[str, Any]:
    """Load and validate guardrails YAML from `path` (default config/guardrails.yaml)."""
    cfg_path = Path(path) if path is not None else DEFAULT_CONFIG_PATH
    if not cfg_path.exists():
        raise GuardrailsConfigError(f"Guardrails config file not found: {cfg_path}")
    try:
        raw = yaml.safe_load(cfg_path.read_text(encoding="utf-8"))
    except Exception as exc:
        raise GuardrailsConfigError(f"Failed parsing {cfg_path}: {exc}") from exc
    return validate_guardrails(raw)


@functools.lru_cache(maxsize=1)
def _cached_default() -> dict[str, Any]:
    return load_guardrails(DEFAULT_CONFIG_PATH)


def get_guardrails(project_root: str | Path | None = None) -> dict[str, Any]:
    """Return cached guardrails config for the default repo root or load a custom root."""
    if project_root is None:
        return _cached_default()
    candidate = Path(project_root) / "config" / "guardrails.yaml"
    if candidate.resolve() == DEFAULT_CONFIG_PATH.resolve():
        return _cached_default()
    if candidate.exists():
        return load_guardrails(candidate)
    return _cached_default()


def reset_cache() -> None:
    """Clear the cached guardrails configuration (for tests)."""
    _cached_default.cache_clear()
