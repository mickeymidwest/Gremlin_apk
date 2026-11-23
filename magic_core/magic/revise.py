"""Reviewer-reviser head -- Magic 5.0 "Hydra" P1.3.

A second pass of the SAME resident 9B critiques a draft answer and, if it finds a
real flaw, rewrites it once. No second model (zero extra VRAM); the cost is one
critic pass (always, on a reviewed turn) plus one rewrite pass (only on a flaw).
Fail-OPEN everywhere: any error or unparseable verdict keeps the original draft,
so the reviser can only help, never block an answer.
"""
from __future__ import annotations

import json
import re

_CRITIC_SYSTEM = (
    "You are a strict reviewer of another assistant's DRAFT answer. Judge only "
    "whether the draft actually answers the user's request correctly, completely, "
    "handles important edge cases, and stays strictly honest: NEVER approve if it "
    "contradicts itself, contradicts retrieved evidence, ignores a stated constraint, "
    "skips code/uses placeholders, or invents a file path, symbol name, command output, "
    "character/killer name, episode/song title, collaboration/rivalry, cause, or figure absent from the provided context. "
    "If Retrieved Evidence is present and the draft asserts specific names, episodes, "
    "collaborations, roles, killers, or dates NOT confirmed in that evidence, you MUST set \"pass\": false "
    "and instruct the rewrite to state ONLY what the evidence supports. "
    "Do NOT nitpick style or tone, and do NOT rewrite it yourself. "
    "Reply with ONE JSON object and nothing else: {\"pass\": true} if it is "
    "good enough to send as-is, or {\"pass\": false, \"flaw\": \"<the single biggest "
    "concrete problem>\", \"fix\": \"<one short instruction to fix it>\"}"
    " (optionally add \"verify\": \"<concise web query>\" when live facts/sources are "
    "needed to verify or fix a claim). When in doubt on style, pass it."
)

_REVISE_SYSTEM = (
    "You are the assistant improving your OWN draft. You get the user's request, your "
    "draft, and one reviewer note (plus any retrieved evidence). Output ONLY the final "
    "reply directly to the user -- fix the noted problem, drop ANY draft claims/names/"
    "episodes/dates that contradict or are absent from the verified evidence, keep only "
    "what is supported (or state plainly what wasn't in the results), and write any "
    "code out in full with no placeholders or elisions. If the retrieved results are "
    "about a different person, show, or fictional character that shares a name with the "
    "user's subject, NEVER accuse the user of confusing them -- just state plainly "
    "that the search results didn't cover the actual subject. Be direct, natural, and "
    "concise; NEVER open with meta-preamble like 'Sure, here is the corrected answer:' "
    "or mention any draft, review, or revision; no robotic lectures, no filler sign-off."
)

_THINK_PAIR_RE = re.compile(r"<(think|thinking)\b[^>]*>.*?</\1>", re.DOTALL | re.IGNORECASE)
_THINK_CLOSE_RE = re.compile(r"</(?:think|thinking)\s*>", re.IGNORECASE)
_PREAMBLE_RE = re.compile(
    r"^(?:\s*(?:sure|okay|alright|got\s+it|certainly|of\s+course)[,!.:\s—–-]*)?"
    r"(?:here(?:'s|\s+is)\s+(?:the\s+|a\s+|my\s+)?(?:full\s+|final\s+)?"
    r"(?:corrected|revised|updated|improved|fixed|cleaned(?:\s+up)?)\s+"
    r"(?:answer|reply|response|version)|(?:corrected|revised|updated)\s+(?:answer|reply|response))"
    r"\s*[:—–-]\s*",
    re.IGNORECASE,
)


def _clean_revised_output(text: str) -> str:
    """Strip leaked <think> blocks and robotic revision preambles."""
    t = _THINK_PAIR_RE.sub("", text or "")
    m = _THINK_CLOSE_RE.search(t)
    if m and not re.search(r"<(?:think|thinking)\b", t[:m.start()], re.IGNORECASE):
        t = t[m.end():]
    t = t.strip()
    t = _PREAMBLE_RE.sub("", t, count=1).strip()
    return t

# GBNF so a grammar-capable backend emits exactly the verdict shape.
_VERDICT_GRAMMAR = (
    'root   ::= "{" ws "\\"pass\\"" ws ":" ws ("true" | "false" rest) ws "}"\n'
    'rest   ::= "," ws "\\"flaw\\"" ws ":" ws string ws "," ws "\\"fix\\"" ws ":" ws string ( ws "," ws "\\"verify\\"" ws ":" ws string )?\n'
    'string ::= "\\"" ( [^"\\\\] | "\\\\" . )* "\\""\n'
    'ws     ::= [ \\t\\n]*\n'
)


def _parse_verdict(text: str):
    m = re.search(r"\{.*\}", text or "", re.DOTALL)
    if not m:
        return None
    try:
        return json.loads(m.group(0))
    except Exception:
        return None


async def _gen(backend, prompt, system, history_msgs, grammar=None,
               max_tokens=1024, temperature=0.3):
    """Call backend.generate, degrading gracefully if it lacks grammar/system/
    history kwargs so this works against any backend."""
    hist = history_msgs or []
    attempts = []
    if grammar is not None:
        attempts.append(dict(system=system, history=hist, grammar=grammar))
    attempts.append(dict(system=system, history=hist))
    attempts.append(dict(system=system))
    attempts.append(dict())
    for kw in attempts:
        try:
            return await backend.generate(prompt, max_tokens=max_tokens,
                                          temperature=temperature, **kw)
        except TypeError:
            continue
    return await backend.generate(prompt, max_tokens=max_tokens, temperature=temperature)


def _add_usage(sink: dict | None, result) -> None:
    if sink is None:
        return
    u = (getattr(result, "meta", None) or {}).get("usage") or {}
    for k, v in u.items():
        if isinstance(v, (int, float)) and isinstance(sink.get(k, 0), (int, float)):
            sink[k] = sink.get(k, 0) + v


async def review_and_revise(backend, question: str, draft: str, *,
                            evidence: str = "", history_msgs=None,
                            lookup_web: bool = False, source_sink: list | None = None,
                            ev_sink: list[str] | None = None,
                            drop_draft_on_fail: bool = False,
                            usage_sink: dict | None = None):
    """(final_text, changed). Critic pass -> on a real flaw, one rewrite pass.
    When `lookup_web=True`, looks up live sources to ground review/fact-checking
    if context is missing or the reviewer flags `"verify"`. Fail-open everywhere."""
    draft = (draft or "").strip()
    if not draft or len((question or "").strip()) == 0:
        return draft, False

    web_ev = ""
    if lookup_web and not (evidence or "").strip():
        try:
            from .. import autoresearch as _ar
            if _ar.needs_fact_check(question, draft) or _ar.should_search(question):
                ws, wev = await _ar.gather(question, force=True)
                if ws and wev:
                    web_ev = wev
                    if source_sink is not None:
                        source_sink.extend(ws)
                    if ev_sink is not None:
                        ev_sink.append(wev)
        except Exception:
            pass

    combined_ev = "\n\n".join(x for x in (evidence, web_ev) if x and x.strip())
    ev = f"\n\nContext the draft could rely on:\n{combined_ev[:4500]}" if combined_ev else ""
    critic_prompt = (f"User's request:\n{question}\n\nThe draft answer:\n{draft}{ev}\n\n"
                     "Does this draft need work? Reply with the JSON object only.")
    try:
        r = await _gen(backend, critic_prompt, _CRITIC_SYSTEM, history_msgs,
                       grammar=_VERDICT_GRAMMAR, max_tokens=256)
    except Exception:
        return draft, False
    _add_usage(usage_sink, r)
    if not getattr(r, "ok", True):
        return draft, False
    verdict = _parse_verdict(getattr(r, "text", "") or "")
    # fail-open: no verdict, or an explicit pass -> keep the draft
    if not isinstance(verdict, dict) or verdict.get("pass") is not False:
        return draft, False
    flaw = str(verdict.get("flaw") or "").strip()[:400]
    fix = str(verdict.get("fix") or "").strip()[:400]
    if not flaw:
        return draft, False

    verify_q = str(verdict.get("verify") or "").strip()[:200]
    if lookup_web and verify_q and verify_q.lower() != question.strip().lower():
        try:
            from .. import autoresearch as _ar
            ws2, wev2 = await _ar.gather(verify_q, force=True)
            if ws2 and wev2:
                web_ev = "\n\n".join(x for x in (web_ev, wev2) if x)
                combined_ev = "\n\n".join(x for x in (evidence, web_ev) if x and x.strip())
                if source_sink is not None:
                    seen_u = {s.get("url") for s in source_sink}
                    source_sink.extend(s for s in ws2 if s.get("url") not in seen_u)
                if ev_sink is not None:
                    ev_sink.append(wev2)
        except Exception:
            pass

    rev_ev = f"\n\nVerified context / web evidence:\n{combined_ev[:4500]}" if combined_ev else ""
    if drop_draft_on_fail and combined_ev:
        # Unsearched hallucinated drafts can anchor the 9B on fake details --
        # rewrite cleanly from the user's question + verified evidence alone.
        revise_prompt = (f"User's request:\n{question}{rev_ev}\n\n"
                         f"Note on earlier wrong draft: {flaw}. {fix}\n\n"
                         "Ignore any earlier guesses and answer directly from the verified evidence. "
                         "If those results are about a different entity sharing a keyword, say plainly "
                         "that the search didn't find sources on the subject instead of lecturing the user.")
    else:
        revise_prompt = (f"User's request:\n{question}\n\nYour draft:\n{draft}{rev_ev}\n\n"
                         f"Reviewer note -- problem: {flaw}\n"
                         f"how to fix: {fix}\n\nReply directly to the user with the fixed answer "
                         "(no preamble like 'Sure, here is the corrected answer:' and no mention of any draft).")
    revise_max_tokens = 2048
    try:
        r2 = await _gen(backend, revise_prompt, _REVISE_SYSTEM,
                        ([] if (drop_draft_on_fail and combined_ev) else history_msgs),
                        max_tokens=revise_max_tokens)
        _add_usage(usage_sink, r2)
        t2 = _clean_revised_output(getattr(r2, "text", "") or "") if getattr(r2, "ok", True) else ""
    except Exception:
        return draft, False
    # For fresh web rewrites, don't reject a concise true answer just because the
    # hallucinated draft was longer; otherwise keep existing length floor.
    min_len = 20 if (drop_draft_on_fail and combined_ev) else 0.35 * len(draft)
    if not t2 or len(t2) < min_len:
        return draft, False
    return t2, True
