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
    "You are the CLASSIFIER specialist. Read the user message and name the single "
    "best-fitting action label, or 'chat' if it needs no tool. Respond with ONLY "
    "the label, nothing else."
)
_CODER_SYSTEM = (
    "You are the CODER specialist. Write correct, minimal code or a concrete, "
    "ordered plan. Be precise about commands, flags, paths and APIs -- never guess "
    "a flag; if unsure, say so."
)

DEFAULT_SPECIALISTS = [
    # Each specialist names the CPU model it runs on. The tiny 1.5B classifier is
    # pinned-resident (cheap); the others share the rotating CPU slot under the
    # resident cap, falling back to the top-dog if their model isn't registered.
    Specialist("watchdog", "keep the top-dog honest -- flag fabricated actions, "
               "wrong-question answers, bad commands, overreach",
               _WATCHDOG_SYSTEM, grammar=_WATCHDOG_SCHEMA, max_tokens=300, budget_s=25.0),
    Specialist("classifier", "decide what the user is asking for (intent)",
               _CLASSIFIER_SYSTEM, model="qwen2.5-1.5b-cpu", max_tokens=40, budget_s=12.0),
    Specialist("coder", "draft code / plans and critique commands and flags",
               _CODER_SYSTEM, model="qwen2.5-coder-3b-cpu", max_tokens=600, budget_s=40.0),
    Specialist("summarizer", "condense text to its core",
               _SUMMARIZER_SYSTEM, max_tokens=250, budget_s=20.0),
    Specialist("critic", "judge whether an answer addresses the question",
               _CRITIC_SYSTEM, grammar=_CRITIC_SCHEMA, max_tokens=200, budget_s=20.0),
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

    def ask(self, name: str, prompt: str, *, extra_system: str = "") -> HiveResult:
        """Run a specialist, bounded to its budget and best-effort. Never raises:
        a missing specialist/model, a timeout, or an error -> ok=False."""
        spec = self._specialists.get(name)
        if spec is None:
            return HiveResult(name=name, ok=False)
        model, used = self._model_for(spec)
        if model is None:
            return HiveResult(name=name, ok=False)
        sysp = spec.system + (("\n\n" + extra_system) if extra_system else "")

        def _run():
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
                return HiveResult(name=name, text=text, ok=bool(text),
                                  secs=round(time.monotonic() - t0, 1), model=used)
            except _cf.TimeoutError:
                # the CPU worker is still generating and holding its lock; stop
                # it so it does not stay wedged for the backend's full timeout.
                try:
                    getattr(getattr(model, "_backend", None), "_stop", lambda: None)()
                except Exception:
                    pass
                return HiveResult(name=name, ok=False, dropped=True,
                                  secs=round(time.monotonic() - t0, 1), model=used)
            except Exception:
                return HiveResult(name=name, ok=False,
                                  secs=round(time.monotonic() - t0, 1), model=used)
        finally:
            ex.shutdown(wait=False, cancel_futures=True)


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


def build_hive(registry, *, loop=None, log=print) -> Hive:
    return Hive(registry, loop=loop, log=log)
