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
