"""Lean tool exposure: the classifier sees only message-relevant tools."""
from magic_core.intent import (_relevant_tool_names, _build_classify_prompt,
                                 _classify_grammar)


def test_surfaces_the_right_tools():
    assert "service_restart" in _relevant_tool_names("restart the jellyfin service")
    assert "read_file" in _relevant_tool_names("read the file config.yaml")


def test_catch_all_always_present():
    # even a message with no lexical overlap keeps the run_command catch-all
    r = _relevant_tool_names("zzzqqq obscure nonsense token")
    assert "run_command" in r


def test_lean_prompt_is_smaller_and_capped():
    rel = _relevant_tool_names("restart jellyfin")
    assert len(rel) <= 7                                   # capped (limit 6 + catch-all)
    assert len(_build_classify_prompt(rel)) < len(_build_classify_prompt())


def test_grammar_enum_matches_shown_tools():
    g = _classify_grammar(["read_file", "run_command"])
    assert set(g["properties"]["action"]["enum"]) == {"read_file", "run_command", "chat"}
    # with no filter it still covers the whole registry (+ chat)
    full = _classify_grammar()
    assert "service_stop" in full["properties"]["action"]["enum"]


def test_shown_tools_are_in_the_grammar():
    # whatever is shown in the prompt is selectable in the grammar (no orphans)
    rel = _relevant_tool_names("stop the battles")
    prompt = _build_classify_prompt(rel)
    enum = set(_classify_grammar(rel)["properties"]["action"]["enum"])
    for name in rel:
        assert name in enum and name in prompt
