"""GBNF grammar constraints for the local llama.cpp backend.

Grammar-constrained decoding forces the model's token *sampling* to obey a
formal grammar at the engine level -- the sampler can only pick a token
that keeps the output grammatically valid, so the model is mathematically
unable to emit anything that breaks the structure. For a weak 7-8B local
model this is the single biggest reliability win available: no truncated
JSON, no conversational preamble before a tool call, no half-closed
brackets, no wandering into prose when a schema was asked for.

This is a Zoid/harness capability, not a pilot decision (see MAGIC.md
"reliability shield"): the harness constrains the *shape* of the output;
the model still chooses the *content* within that shape. A grammar never
decides the answer -- it only refuses malformed ones.

Only the local LlamaCppBackend can honor a grammar. Remote API backends
(Claude, Gemini, OpenAI-compatible) accept the argument and ignore it --
they are fallbacks and have their own structured-output modes; a GBNF
string is meaningless to them.

Usage (a spec is a GBNF string, a JSON-schema dict, or a prebuilt
LlamaGrammar):

    from gremlin_core.backends import grammars
    r = await backend.generate(prompt, grammar=grammars.JSON_OBJECT)
    r = await backend.generate(prompt, grammar={"type": "object",
            "properties": {"verdict": {"type": "string"}},
            "required": ["verdict"]})

`compile()` returns None whenever grammars can't apply (llama_cpp absent,
or spec is None), so backend code can call it unconditionally and treat
None as "no constraint" -- a clean no-op, never an error.
"""
from __future__ import annotations

import json
from typing import Any, Optional

try:  # same guard shape as llamacpp_backend's `Llama` import
    from llama_cpp import LlamaGrammar
except Exception:  # pragma: no cover - library/CUDA build not present
    LlamaGrammar = None  # type: ignore

# A strict JSON-value grammar (the canonical llama.cpp json.gbnf). Use it
# when you want "valid JSON, any shape"; use a JSON-schema dict when you
# want a *specific* shape (keys, types, required fields).
JSON_OBJECT = r"""
root   ::= object
value  ::= object | array | string | number | ("true" | "false" | "null") ws
object ::= "{" ws ( string ":" ws value ("," ws string ":" ws value)* )? "}" ws
array  ::= "[" ws ( value ("," ws value)* )? "]" ws
string ::= "\"" ( [^"\\\x00-\x1F] | "\\" (["\\/bfnrt] | "u" [0-9a-fA-F] [0-9a-fA-F] [0-9a-fA-F] [0-9a-fA-F]) )* "\"" ws
number ::= ("-"? ([0-9] | [1-9] [0-9]*)) ("." [0-9]+)? ([eE] [-+]? [0-9]+)? ws
ws     ::= ([ \t\n] ws)?
"""

# Compiled-grammar cache. Compiling GBNF is not free and the ReAct loop
# asks for the same grammar on every step, so key compiled grammars by
# their spec and reuse. Bounded so a caller generating many ad-hoc schemas
# can't grow it without limit.
_cache: "dict[str, Any]" = {}
_CACHE_MAX = 64


def _key(spec: Any) -> Optional[str]:
    if isinstance(spec, str):
        return "g:" + spec
    if isinstance(spec, dict):
        try:
            return "s:" + json.dumps(spec, sort_keys=True)
        except (TypeError, ValueError):
            return None
    return None


def compile(spec: Any) -> Any:
    """spec -> a LlamaGrammar, or None when no grammar applies.

    spec may be:
      * None               -> None (no constraint)
      * a LlamaGrammar     -> returned unchanged (already compiled)
      * a str              -> treated as a raw GBNF grammar
      * a dict             -> treated as a JSON schema, converted to GBNF

    Returns None (never raises) if llama_cpp isn't importable or the spec
    can't be compiled, so the backend degrades to unconstrained generation
    rather than failing the request.
    """
    if spec is None or LlamaGrammar is None:
        return None
    # Already a compiled grammar (duck-typed to avoid importing the exact
    # class for an isinstance check when the library layout shifts).
    if not isinstance(spec, (str, dict)):
        return spec

    key = _key(spec)
    if key is not None and key in _cache:
        return _cache[key]

    try:
        if isinstance(spec, dict):
            grammar = LlamaGrammar.from_json_schema(json.dumps(spec))
        else:
            grammar = LlamaGrammar.from_string(spec, verbose=False)
    except Exception:  # a malformed grammar must not sink the generation
        return None

    if key is not None:
        if len(_cache) >= _CACHE_MAX:
            _cache.clear()  # cheap bound; these are rebuilt on demand
        _cache[key] = grammar
    return grammar


# --- ReAct turn grammar (battle.py's text protocol) -------------------
#
# The battle loop drives tools with a text protocol: a turn is an optional
# <think> reasoning block, then EITHER
#     ACTION: <tool>\n```json\n{...}\n```
# with nothing after, OR
#     DONE <free answer>
# A weak local model's worst failures here are malformed JSON args, an
# invented tool name, a missing fence, or trailing hallucinated tool
# output ("user: RESULT: ..."). This grammar makes all of those
# unsamplable: the tool name must be one of the *currently allowed* tools,
# the args must be a well-formed JSON object inside the fence, and the
# turn ends at the closing fence (so there is no "text after the JSON").
#
# Why a <think> slot and not a free prose prelude: GBNF constrains
# left-to-right with no lookahead, so a rule that allows "any text, then a
# required ACTION" can't actually force the ACTION -- every structured
# token is also valid prose, so the constraint collapses. A labelled
# <think> block is the standard resolution: reasoning stays possible, and
# it's already what the llama.cpp backend strips from the visible answer.

_REACT_JSON_SUB = r"""
obj    ::= "{" ws ( kv ( ws "," ws kv )* )? ws "}"
kv     ::= jstr ws ":" ws val
val    ::= obj | arr | jstr | num | "true" | "false" | "null"
arr    ::= "[" ws ( val ( ws "," ws val )* )? ws "]"
jstr   ::= "\"" ( [^"\\] | "\\" (["\\/bfnrt] | "u" hex hex hex hex) )* "\""
num    ::= "-"? ("0" | [1-9] [0-9]*) ("." [0-9]+)? ([eE] [-+]? [0-9]+)?
hex    ::= [0-9a-fA-F]
ws     ::= [ \t\n]*
"""

# the toolhost's full tool vocabulary -- the alternation is narrowed per
# battle to whatever is actually allowed (phase-gating unlocks edits later)
REACT_TOOLS = ("repo_map", "grep", "run_shell", "read_file", "view_file",
               "edit_file", "write_file", "undo_last", "list_dir")


def react_turn_grammar(tools=REACT_TOOLS) -> str:
    """Build the GBNF for one ReAct turn, with the tool-name alternation
    restricted to `tools` (pass toolhost.allowed so a locked tool can't be
    named). Returns a GBNF string; feed it to compile() / generate()."""
    names = [t for t in tools if t in REACT_TOOLS] or ["read_file"]
    alts = " | ".join('"%s"' % t for t in names)
    return (
        'root    ::= think? ws turn\n'
        'think   ::= "<think>" tbody "</think>" ws\n'
        'tbody   ::= ( [^<] | "<" [^/] )*\n'
        'turn    ::= action | done\n'
        'action  ::= "ACTION: " tool "\\n" "```json" "\\n" ws obj ws "\\n" "```"\n'
        'done    ::= "DONE" donesep rest\n'
        'donesep ::= ( ":" | "." )? ( " " | "\\n" )\n'
        'rest    ::= rchar*\n'
        'rchar   ::= [^\\n] | "\\n"\n'
        f'tool    ::= {alts}\n'
        + _REACT_JSON_SUB
    )
