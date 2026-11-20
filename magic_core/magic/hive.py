"""The hive: Magic's CPU back-harness as a team of SPECIALISTS.

One GPU top-dog does the heavy reasoning and the user-facing answers; the hive is
the lane of small CPU models (zero VRAM, running CONCURRENTLY) each built for ONE
job. 8GB VRAM means the top-dog is the only resident model, so the specialists
live on the CPU helper (qwen3.5-4b-cpu) and never fight it for the card.

A `Specialist` is a declarative role: name, purpose, system prompt, the model it
runs on, an optional grammar (JSON schema / GBNF), a token cap, and a wall-clock
budget. `Hive.ask()` routes a job to a specialist and runs it BOUNDED and
BEST-EFFORT: a slow specialist is dropped at its budget and a missing CPU model
falls back to the top-dog, so a specialist can ADD value but never block or break
the top-dog -- the same law the two-head planner follows.

The existing CPU roles (classifier, plan-drafter, reflector, skill-hardener) are
hive members wired in their own modules; this registry formalizes the team and
makes a NEW specialist (e.g. the watchdog) a declaration instead of bespoke
plumbing. Never raises.
"""
from __future__ import annotations

import concurrent.futures as _cf
import json as _json
import re as _re
import time
from dataclasses import dataclass, field
from typing import Optional, Union

from .model import BackendModel

# IMMUTABLE LAW (Mickey 2026-11-13): every loop and rule in this CPU back-harness
# is fixed -- it NEVER changes, even in override/private mode. The specialists'
# rules, the bounded+best-effort contract, and above all the watchdog's job of
# keeping the top-dog honest are rails, not content gates, so override never
# drops them. A specialist can ADD value but never block or break the top-dog,
# and override can relax what the top-dog may SAY, never what the hive CHECKS.


@dataclass
class Specialist:
    name: str
    purpose: str
    system: str
    model: str = "qwen3.5-4b-cpu"          # the CPU model this role runs on
    grammar: Optional[Union[dict, str]] = None
    max_tokens: int = 500
    temperature: float = 0.3
    budget_s: float = 20.0                  # wall-clock; dropped if it overruns
    strict: bool = False                    # True = self-edit/review lane: fail closed on bad JSON/errors


@dataclass
class HiveResult:
    name: str
    text: str = ""
    ok: bool = False
    secs: float = 0.0
    dropped: bool = False                   # True = exceeded its budget
    model: str = ""


# --- the watchdog: the hive member whose whole job is keeping the top-dog honest
_WATCHDOG_SCHEMA = {
    "type": "object",
    "properties": {
        "ok": {"type": "boolean"},
        "concerns": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["ok", "concerns"],
}
_WATCHDOG_SYSTEM = (
    "You are the WATCHDOG specialist in an AI harness. You review the assistant's "
    "draft answer to the user and flag ONLY concrete problems, not style. Flag: "
    "(1) any claim of having run a command, scanned, or checked a file/service "
    "when it could not have; (2) answering a DIFFERENT question than the one "
    "asked; (3) a specific command/flag/path asserted as fact that looks wrong; "
    "(4) overreach beyond what was asked. If the answer is fine, say so. Respond "
    'with ONLY JSON: {"ok": true/false, "concerns": ["short concern", ...]}. '
    "Empty concerns when ok is true."
)

_SUMMARIZER_SYSTEM = (
    "You are the SUMMARIZER specialist. Condense the text to its core in at most "
    "3 short sentences. No preamble, just the summary."
)

_CRITIC_SYSTEM = (
    "You are the CRITIC specialist. Given a user question and the assistant's "
    "answer, judge ONLY whether the answer actually addresses the question asked. "
    'Respond with ONLY JSON: {"addresses": true/false, "why": "one short line"}.'
)
_CRITIC_SCHEMA = {
    "type": "object",
    "properties": {"addresses": {"type": "boolean"}, "why": {"type": "string"}},
    "required": ["addresses", "why"],
}


_CLASSIFIER_SYSTEM = (
    "You are the CLASSIFIER specialist. Read the user message and choose the single "
    "best-fitting action label, or 'chat' if it needs no tool. Respond with ONLY "
    'JSON: {"label": "<action-or-chat>", "reason": "short phrase"}.'
)
_CLASSIFIER_SCHEMA = {
    "type": "object",
    "properties": {"label": {"type": "string"}, "reason": {"type": "string"}},
    "required": ["label"],
}
_CODER_SYSTEM = (
    "You are the CODER specialist. Write an exact, minimal ordered plan and verified "
    "commands/files. Never guess a flag or path; mark unknowns clearly. Respond with "
    'ONLY JSON: {"plan": ["..."], "commands": ["..."], "warnings": ["..."]}.'
)
_CODER_SCHEMA = {
    "type": "object",
    "properties": {
        "plan": {"type": "array", "items": {"type": "string"}},
        "commands": {"type": "array", "items": {"type": "string"}},
        "warnings": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["plan"],
}
_AUDIT_SYSTEM = (
    "You are the PATCH AUDITOR specialist guarding self-edits and critical reviews. "
    "Enforce strict fail-closed rules: BLOCK any patch that (1) weakens safety, tests, "
    "sandboxing, grounding, or honesty checks; (2) violates project decisions (revives "
    "consult/council/specialists/bench/distill, adds fine-tuning/SFTTrainer, deletes "
    "tracked files instead of moving to .magic/trash, loads two local GGUFs at once, "
    "or runs heavy tests outside scripts/heavy.sh); or (3) introduces logic/cross-module "
    "bugs. Respond with ONLY JSON matching the schema."
)
_AUDIT_SCHEMA = {
    "type": "object",
    "properties": {
        "verdict": {"type": "string", "enum": ["APPROVE", "BLOCK"]},
        "rule_violations": {"type": "array", "items": {"type": "string"}},
        "blocking_bugs": {"type": "array", "items": {"type": "string"}},
        "risk": {"type": "string", "enum": ["low", "med", "high"]},
    },
    "required": ["verdict", "rule_violations", "blocking_bugs", "risk"],
}

_WEB_FACT_SCHEMA = {
    "type": "object",
    "properties": {
        "ok": {"type": "boolean"},
        "unsupported": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["ok", "unsupported"],
}
_WEB_FACT_SYSTEM = (
    "You are the WEB FACT CHECKER specialist. Compare the assistant's draft reply "
    "against ONLY the provided raw web search snippets and fetched source text. "
    "Flag any specific person/character/entity name, date/year, or plot/event claim "
    "stated as fact in the draft that is NOT present in or supported by the source text. "
    "Ignore general wording, uncertainty/hedging ('I couldn't verify'), or facts from the user's question itself. "
    'Respond with ONLY JSON: {"ok": true/false, "unsupported": ["specific unverified claim", ...]}. '
    "Return ok=true and an empty unsupported list when every specific claim is backed by the source text."
)

DEFAULT_SPECIALISTS = [
    # Each specialist names the CPU model it runs on. The tiny 1.5B classifier is
    # pinned-resident (cheap); the others share the rotating CPU slot under the
    # resident cap, falling back to the top-dog if their model isn't registered.
    Specialist("watchdog", "keep the top-dog honest -- flag fabricated actions, "
               "wrong-question answers, bad commands, overreach",
               _WATCHDOG_SYSTEM, grammar=_WATCHDOG_SCHEMA, max_tokens=300, budget_s=25.0),
    Specialist("classifier", "decide what the user is asking for (intent)",
               _CLASSIFIER_SYSTEM, model="qwen2.5-1.5b-cpu", grammar=_CLASSIFIER_SCHEMA,
               max_tokens=80, budget_s=12.0),
    Specialist("coder", "draft code / plans and critique commands and flags",
               _CODER_SYSTEM, model="qwen2.5-coder-3b-cpu", grammar=_CODER_SCHEMA,
               max_tokens=600, budget_s=40.0),
    Specialist("patch_auditor", "fail-closed self-edit & decision-rule patch auditor",
               _AUDIT_SYSTEM, model="qwen2.5-coder-3b-cpu", grammar=_AUDIT_SCHEMA,
               max_tokens=400, temperature=0.1, budget_s=30.0, strict=True),
    Specialist("summarizer", "condense text to its core",
               _SUMMARIZER_SYSTEM, max_tokens=250, budget_s=20.0),
    Specialist("critic", "judge whether an answer addresses the question",
               _CRITIC_SYSTEM, grammar=_CRITIC_SCHEMA, max_tokens=200, budget_s=20.0),
    Specialist("web_fact_checker", "compare draft reply against raw search snippets "
               "for unverified names, dates, or plot claims",
               _WEB_FACT_SYSTEM, model="qwen3.5-4b-cpu", grammar=_WEB_FACT_SCHEMA,
               max_tokens=250, temperature=0.1, budget_s=15.0),
]


class Hive:
    """Holds the specialists and runs them on the CPU lane, bounded + best-effort."""

    def __init__(self, registry, *, loop=None, log=print, specialists=None):
        self._registry = registry
        self._loop = loop
        self._log = log
        self._specialists: dict = {}
        for s in (specialists if specialists is not None else DEFAULT_SPECIALISTS):
            self.register(s)

    def register(self, spec: Specialist) -> None:
        self._specialists[spec.name] = spec

    def names(self) -> list:
        return list(self._specialists)

    def get(self, name: str) -> Optional[Specialist]:
        return self._specialists.get(name)

    def roster(self) -> str:
        return "\n".join(f"- {s.name}: {s.purpose} (on {s.model}, {int(s.budget_s)}s)"
                         for s in self._specialists.values())

    def _primary_name(self) -> Optional[str]:
        try:
            return self._registry.primary_model_name()
        except Exception:
            return "magic"

    def _model_for(self, spec: Specialist):
        """A Model for this specialist -- ONLY its CPU model. No GPU-primary
        fallback: a specialist lives on the CPU lane and must never queue
        background work onto the GPU the top-dog is using. If its model is not
        registered, the specialist is simply unavailable (caller skips)."""
        try:
            backend = self._registry.get(spec.model)
        except Exception:
            backend = None
        if backend is None:
            return None, None
        return BackendModel(backend, loop=self._loop, temperature=spec.temperature), spec.model

    def ask(self, name: str, prompt: str, *, extra_system: str = "",
            strict: Optional[bool] = None) -> HiveResult:
        """Run a specialist, bounded to its budget. Default is fail-open for chat;
        pass `strict=True` (or set `spec.strict`) to validate JSON schema and fail
        closed for self-edit/review gates."""
        spec = self._specialists.get(name)
        if spec is None:
            return HiveResult(name=name, ok=False)
        is_strict = spec.strict if strict is None else strict
        model, used = self._model_for(spec)
        if model is None:
            return HiveResult(name=name, ok=False)
        sysp = spec.system + (("\n\n" + extra_system) if extra_system else "")

        def _run():
            try:
                reply = model.complete([{"role": "user", "content": prompt}],
                                       system=sysp, max_tokens=spec.max_tokens,
                                       grammar=spec.grammar, timeout=spec.budget_s)
            except TypeError:
                reply = model.complete([{"role": "user", "content": prompt}],
                                       system=sysp, max_tokens=spec.max_tokens,
                                       grammar=spec.grammar)
            return (getattr(reply, "text", "") or "").strip()

        t0 = time.monotonic()
        ex = _cf.ThreadPoolExecutor(max_workers=1)
        try:
            fut = ex.submit(_run)
            try:
                text = fut.result(timeout=spec.budget_s)
                ok = bool(text)
                if ok and is_strict and isinstance(spec.grammar, dict):
                    try:
                        parsed = _json.loads(text)
                        ok = isinstance(parsed, dict)
                    except Exception:
                        ok = False
                return HiveResult(name=name, text=text, ok=ok,
                                  secs=round(time.monotonic() - t0, 1), model=used)
            except _cf.TimeoutError:
                # Never call backend._stop() unlocked from the caller thread:
                # CpuChatBackend._request enforces `timeout=spec.budget_s` and
                # tears down its own timed-out subprocess inside self._lock.
                return HiveResult(name=name, ok=False, dropped=True,
                                  secs=round(time.monotonic() - t0, 1), model=used)
            except Exception as e:
                is_timeout = "timed out" in str(e).lower()
                return HiveResult(name=name, ok=False, dropped=is_timeout,
                                  secs=round(time.monotonic() - t0, 1), model=used)
        finally:
            ex.shutdown(wait=False, cancel_futures=True)

    def audit_patch(self, goal: str, patch: str, decisions_text: str = "", *,
                    require_model: bool = False) -> tuple[bool, list[str], str]:
        """Hardened fail-closed self-edit/patch review:
          1) Deterministic pregate protected-path, safety-token, & decisions rules.
          2) Strict JSON `patch_auditor` specialist when its CPU model is present
             (or required if `require_model=True`).
        Returns `(approved, issues, summary)`."""
        issues: list[str] = []
        try:
            from .. import pregate
            issues.extend(pregate._protected_path_edits(patch))
            issues.extend(pregate._safety_regressions(patch))
            issues.extend(pregate.decision_violations(patch))
        except Exception as e:
            return False, [f"deterministic pregate audit failed closed: {e}"], "blocked"
        if issues:
            return False, issues, f"blocked by {len(issues)} deterministic rule(s)"

        spec = self.get("patch_auditor")
        model, _used = self._model_for(spec) if spec is not None else (None, None)
        if model is None:
            if require_model:
                return False, ["patch_auditor model unavailable (fail-closed)"], "blocked"
            return True, [], "deterministic checks passed (auditor model skipped)"

        extra = f"KEY DECISIONS TO ENFORCE:\n{decisions_text[:1500]}" if decisions_text else ""
        prompt = f"Goal:\n{goal}\n\nDiff to audit:\n{patch[:8000]}"
        res = self.ask("patch_auditor", prompt, extra_system=extra, strict=True)
        if not res.ok:
            why = "patch_auditor timed out" if res.dropped else "patch_auditor returned unusable output"
            return False, [why], "blocked"
        try:
            data = _json.loads(res.text)
        except Exception as e:
            return False, [f"patch_auditor JSON parse error: {e}"], "blocked"

        verdict = str(data.get("verdict", "")).upper()
        found = [str(x).strip() for x in (data.get("rule_violations") or []) + (data.get("blocking_bugs") or []) if str(x).strip()]
        if verdict != "APPROVE" or found:
            return False, (found or [f"patch_auditor verdict={verdict!r} (risk={data.get('risk', '?')})"]), "blocked"
        return True, [], f"approved (risk={data.get('risk', 'low')})"


    def watchdog_review(self, message: str, answer: str) -> list:
        """Run the watchdog on one (question, answer) pair; return its concerns
        (empty when clean or unavailable). This is an IMMUTABLE honesty rail --
        it runs the same in override/private as anywhere else."""
        prompt = (f"User asked:\n{message}\n\nAssistant answered:\n{answer}\n\n"
                  "Review the answer per your rules.")
        r = self.ask("watchdog", prompt)
        if not r.ok or not r.text:
            return []
        try:
            m = _re.search(r"\{.*\}", r.text, _re.DOTALL)
            if not m:
                return []
            d = _json.loads(m.group(0))
            if d.get("ok") is True:
                return []
            return [str(c) for c in (d.get("concerns") or []) if str(c).strip()][:5]
        except Exception:
            return []

    def check_web_facts(self, draft: str, source_text: str, question: str = "") -> list[str]:
        """Compare `draft` against `source_text` via qwen3.5-4b-cpu and return any
        specific name, date, or plot claim not present in the source text."""
        if not (draft or "").strip() or not (source_text or "").strip():
            return []
        q_part = f"User question:\n{question.strip()[:400]}\n\n" if (question or "").strip() else ""
        prompt = (
            f"{q_part}"
            f"Raw search snippets / source text:\n{source_text.strip()[:4500]}\n\n"
            f"Draft reply:\n{draft.strip()[:2500]}\n\n"
            "List any specific name, date, or plot claim in the draft reply not supported by the source text."
        )
        r = self.ask("web_fact_checker", prompt)
        if not r.ok or not r.text:
            return []
        try:
            m = _re.search(r"\{.*\}", r.text, _re.DOTALL)
            if not m:
                return []
            d = _json.loads(m.group(0))
            if d.get("ok") is True:
                return []
            return [str(c).strip() for c in (d.get("unsupported") or []) if str(c).strip()][:5]
        except Exception:
            return []


def build_hive(registry, *, loop=None, log=print) -> Hive:
    return Hive(registry, loop=loop, log=log)
