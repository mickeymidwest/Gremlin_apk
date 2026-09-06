"""Magic RECKONING: between battles (MAGIC.md section 3).

    score  -> diagnose -> propose (candidate skills + facts) -> gate

§14 keeps it to two model roles: one call proposes, a second, independent
call gates each proposal (well-formed? not a duplicate? not contradicting
a fact?). No adversarial "counter" loop, no meta-campaign -- §12.
"""
from __future__ import annotations

import json
import re
import uuid
from typing import Sequence

from .model import Model
from .types import BattleResult, Fact, Proposal, Skill

_PROPOSE_SYSTEM = """\
You review one attempt an agent made at a coding task and decide what it
should learn. You are NOT re-doing the task. Output STRICT JSON:

{
  "diagnosis": "1-3 sentences: the decisive move (right or wrong) and the CLASS of situation it belongs to",
  "proposals": [
    {"kind": "new_skill", "name": "kebab-case-name", "purpose": "one line",
     "trigger_when": "when this applies", "trigger_matcher": "optional regex or \\"\\"",
     "procedure": ["step", "step"]},
    {"kind": "revise_skill", "target": "existing-skill-name", "purpose": "optional new one line",
     "trigger_when": "optional", "trigger_matcher": "optional", "procedure": ["improved step", "step"]},
    {"kind": "new_fact", "text": "a durable fact, still true next week"}
  ]
}

Rules:
- 0-3 proposals. Fewer is better. Only propose what a repeated pattern justifies.
- A skill is a THING TO DO. A fact is a THING TO KNOW. Do not blur them.
- Do not propose a skill that just restates one already listed below.
- revise_skill REPLACES a listed skill's procedure with a better one -- the old
  version is retired and the revision must re-earn its place. Use it only when a
  listed skill is aimed right but its steps are wrong, incomplete, or were just
  shown to fail; never for a brand-new capability (that is new_skill).
- No prose outside the JSON.
"""

_GATE_SYSTEM = """\
You are a gate. Given one proposed change and the current skills/facts,
answer STRICT JSON: {"accept": true|false, "reason": "short"}.

Reject if: it is malformed or vague; it duplicates an existing skill or
fact; a skill's procedure is not actionable; it contradicts an existing
fact without explaining why. When in doubt, reject -- a missed skill is
cheaper than a bad one.
"""

_JSON_RE = re.compile(r"\{.*\}", re.DOTALL)


def _extract_json(text: str) -> dict:
    m = _JSON_RE.search(text or "")
    if not m:
        return {}
    try:
        return json.loads(m.group(0))
    except (ValueError, TypeError):
        return {}


def _render_context(skills: Sequence[Skill], facts: Sequence[Fact]) -> str:
    lines = ["EXISTING SKILLS:"]
    lines += [f"- {s.name} [{s.status}]: {s.purpose}" for s in skills] or ["  (none)"]
    lines.append("\nEXISTING FACTS:")
    lines += [f"- {f.text}" for f in facts] or ["  (none)"]
    return "\n".join(lines)


def _transcript_digest(result: BattleResult, limit: int = 6000) -> str:
    lines = [f"TASK OUTCOME: score {result.score.value:.2f}"]
    if result.score.failure_signal:
        lines.append(f"failure signal: {result.score.failure_signal}")
    lines.append("\nSTEPS:")
    for st in result.transcript.steps:
        if st.kind == "model":
            lines.append(f"[agent] {st.content.strip()[:600]}")
        elif st.kind == "tool":
            lines.append(f"[tool {st.tool_name} -> {st.content}] {(st.tool_result or '')[:400]}")
    lines.append(f"\nFINAL: {result.transcript.final_message}")
    return "\n".join(lines)[:limit]


def reckon(model: Model, result: BattleResult,
           skills: Sequence[Skill], facts: Sequence[Fact]) -> list[Proposal]:
    prompt = (
        _render_context(skills, facts)
        + "\n\n---\nAN ATTEMPT TO REVIEW:\n"
        + _transcript_digest(result)
    )
    reply = model.complete([{"role": "user", "content": prompt}],
                           system=_PROPOSE_SYSTEM, max_tokens=2000)
    data = _extract_json(reply.text)
    live_skill_names = {s.name for s in skills if s.status != "deprecated"}
    out: list[Proposal] = []
    for raw in data.get("proposals", [])[:3]:
        kind = raw.get("kind")
        if kind == "new_skill" and raw.get("name") and raw.get("procedure"):
            out.append(Proposal(kind="new_skill", payload={
                "name": _slug(raw["name"]),
                "purpose": raw.get("purpose", ""),
                "trigger_when": raw.get("trigger_when", ""),
                "trigger_matcher": raw.get("trigger_matcher") or None,
                "procedure": [str(s) for s in raw["procedure"] if str(s).strip()],
            }, rationale=data.get("diagnosis", "")))
        elif kind == "revise_skill" and raw.get("target") and raw.get("procedure"):
            target = _slug(raw["target"])
            if target not in live_skill_names:
                continue  # can only revise a skill that currently exists and isn't deprecated
            out.append(Proposal(kind="revise_skill", payload={
                "target": target,
                "purpose": raw.get("purpose", ""),
                "trigger_when": raw.get("trigger_when", ""),
                "trigger_matcher": raw.get("trigger_matcher") or None,
                "procedure": [str(s) for s in raw["procedure"] if str(s).strip()],
            }, rationale=data.get("diagnosis", "")))
        elif kind == "new_fact" and raw.get("text"):
            out.append(Proposal(kind="new_fact", payload={"text": raw["text"].strip()},
                                rationale=data.get("diagnosis", "")))
    return out


def gate(model: Model, proposals: Sequence[Proposal],
         skills: Sequence[Skill], facts: Sequence[Fact]) -> list[Proposal]:
    ctx = _render_context(skills, facts)
    kept: list[Proposal] = []
    for p in proposals:
        prompt = f"{ctx}\n\n---\nPROPOSED {p.kind}:\n{json.dumps(p.payload, indent=2)}\n\nrationale: {p.rationale}"
        if p.kind == "revise_skill":
            cur = next((s for s in skills if s.name == p.payload.get("target")
                        and s.status != "deprecated"), None)
            if cur is not None:
                prompt += f"\n\nCURRENT VERSION being replaced:\n{cur.render()}"
        reply = model.complete([{"role": "user", "content": prompt}],
                               system=_GATE_SYSTEM, max_tokens=300)
        verdict = _extract_json(reply.text)
        if verdict.get("accept") is True:
            kept.append(p)
    return kept


def apply_proposals(proposals: Sequence[Proposal], battle_id: str,
                    skills: list[Skill], facts: list[Fact]) -> int:
    """Mutates `skills` / `facts` in place. Returns how many landed."""
    n = 0
    existing_skill_names = {s.name for s in skills}
    existing_fact_texts = {f.text.lower() for f in facts}
    for p in proposals:
        if p.kind == "new_skill":
            if p.payload["name"] in existing_skill_names:
                continue
            skills.append(Skill(
                id="skill_" + uuid.uuid4().hex[:8],
                name=p.payload["name"], purpose=p.payload["purpose"],
                trigger_when=p.payload["trigger_when"],
                trigger_matcher=p.payload["trigger_matcher"],
                procedure=p.payload["procedure"],
                provenance=[battle_id], status="candidate",
            ))
            existing_skill_names.add(p.payload["name"])
            n += 1
        elif p.kind == "revise_skill":
            old = next((s for s in skills if s.name == p.payload["target"]
                        and s.status != "deprecated"), None)
            if old is None:
                continue
            old.status = "deprecated"
            tm = p.payload["trigger_matcher"]
            skills.append(Skill(
                id="skill_" + uuid.uuid4().hex[:8],
                name=old.name,
                purpose=p.payload["purpose"] or old.purpose,
                trigger_when=p.payload["trigger_when"] or old.trigger_when,
                trigger_matcher=tm if tm is not None else old.trigger_matcher,
                procedure=p.payload["procedure"],
                provenance=list(old.provenance) + [battle_id],
                supersedes=old.id,
                status="candidate",
            ))
            n += 1
        elif p.kind == "new_fact":
            if p.payload["text"].lower() in existing_fact_texts:
                continue
            facts.append(Fact(id="fact_" + uuid.uuid4().hex[:8],
                              text=p.payload["text"], provenance=[battle_id]))
            existing_fact_texts.add(p.payload["text"].lower())
            n += 1
    return n


def _slug(s: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", s.lower()).strip("-")
    return s or "unnamed-skill"
