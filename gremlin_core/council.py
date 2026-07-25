"""
The council: Gremlin's own five models choose the specialist network.

Rather than a human picking twenty sub-models, the primary and the four
consult models each propose specialists for the tasks they think matter,
argue for them, and converge on a roster. They're the ones who'll be
routing work to these, so they get a say in what's in the network.

**The one thing this module exists to solve.** Ask any LLM for Hugging
Face repo IDs and it will produce confident, plausible, non-existent
names. That's not a hypothetical: earlier in this project a proposed
`huihui-ai/Llama-3-8B-Instruct-Abliterated-GGUF` turned out not to
exist at all, and only a real API call caught it. Five models proposing
twenty repos each will generate a great deal of fiction.

So a proposal is never a selection. Every candidate is checked against
the real Hugging Face API -- does the repo exist, does it actually
contain GGUF files, what do those files really weigh -- and anything
that fails is dropped and *reported back to the council with the
reason*, so the next round is informed rather than repeating itself.
The council proposes; the API decides.

**Uncensored requirement** (mickey's stipulation): enforced as a
name/tag heuristic -- "abliterated", "uncensored", "dolphin", etc. This
is honestly a heuristic, not a guarantee: there is no API field for
"this model will not refuse", and the only real test is running it.
Candidates are labelled with *why* they were considered uncensored so
that judgement stays visible rather than implied.

Nothing here writes to config/models.yaml. The roster lands in
data/council_roster.json as a proposal for a human to act on -- twenty
extra models is a big change to make behind someone's back, and on an
8GB card they cannot all be resident anyway (see the VRAM note in the
generated roster).
"""
from __future__ import annotations

import json
import re
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Optional

import requests

from . import hf_hub
from .registry import ModelRegistry
from .router import Router
from .specialists import TaskType

REASON_MISSING = "repo does not exist on Hugging Face (or is private)"

DEFAULT_TARGET = 20
DEFAULT_ROUNDS = 3

# Markers that a model has had refusal behavior removed or was trained
# without it. A heuristic on names/tags -- see module docstring.
_UNCENSORED_MARKERS = [
    "abliterated", "uncensored", "unfiltered", "dolphin", "unaligned",
    "no-refusal", "norefusal", "amoral", "neural-chat", "openhermes",
    "nous-hermes", "wizard-vicuna-uncensored", "lewd", "unhinged",
]

_PROPOSE_SYSTEM = """You are one member of a council of models choosing specialist sub-models to
join your own network. You will route real work to these, so choose for usefulness, not novelty.

HARD REQUIREMENTS:
1. Every model MUST be uncensored/abliterated (names usually contain "abliterated",
   "uncensored", "unfiltered", "dolphin", or similar). A model that refuses requests is useless here.
2. Every model MUST exist on Hugging Face as a GGUF repo. Do NOT invent repo names.
   If you are not certain a repo exists, say so in "confidence" rather than guessing --
   every name you give is checked against the real Hugging Face API, and inventions are
   discarded and shown back to you.
3. Prefer SMALL specialists (0.5B-8B). The point is many focused models, not more big ones.

Respond with ONLY a JSON array, no prose, no markdown fence:
[{"repo": "<org/name-GGUF>", "task_type": "<vision|code|math|extraction|classification|general>",
  "why": "<one sentence: what it's for>", "confidence": <0.0-1.0 that this repo really exists>}]
"""


@dataclass
class Candidate:
    repo: str
    task_type: str
    why: str
    proposed_by: str
    confidence: float = 0.0
    # Filled in by verification against the real API.
    exists: bool = False
    gguf_count: int = 0
    smallest_gguf_bytes: int = 0
    uncensored_evidence: str = ""
    rejected_reason: str = ""

    @property
    def accepted(self) -> bool:
        return self.exists and self.gguf_count > 0 and bool(self.uncensored_evidence)


def _uncensored_evidence(repo: str, tags: list[str]) -> str:
    """Why we believe this is uncensored, or '' if we don't."""
    haystack = (repo + " " + " ".join(tags)).lower()
    hits = [m for m in _UNCENSORED_MARKERS if m in haystack]
    return f"name/tags contain: {', '.join(hits)}" if hits else ""


def verify_candidate(cand: Candidate, session: Optional[requests.Session] = None) -> Candidate:
    """Check one proposal against the real Hugging Face API.

    This is the load-bearing function of the module: it's what separates
    a model's confident guess from a model that exists."""
    repo = (cand.repo or "").strip().strip("/")
    if not repo or repo.count("/") != 1:
        cand.rejected_reason = "not a valid org/name repo id"
        return cand

    s = session or requests
    try:
        resp = s.get(f"https://huggingface.co/api/models/{repo}", timeout=15)
    except Exception as e:
        cand.rejected_reason = f"lookup failed: {e}"
        return cand

    # 401 as well as 404: Hugging Face answers 401 for a repo that is
    # private *or* simply doesn't exist, deliberately not distinguishing
    # the two. Verified live -- an invented repo id returns 401, not 404.
    # Treating only 404 as "missing" would misfile every hallucinated
    # name as a generic HTTP error and undercount them in the summary.
    if resp.status_code in (401, 403, 404):
        cand.rejected_reason = REASON_MISSING
        return cand
    if resp.status_code != 200:
        cand.rejected_reason = f"lookup returned HTTP {resp.status_code}"
        return cand

    try:
        meta = resp.json()
    except ValueError:
        cand.rejected_reason = "malformed API response"
        return cand

    cand.exists = True
    tags = [str(t) for t in (meta.get("tags") or [])]
    cand.uncensored_evidence = _uncensored_evidence(repo, tags)
    if not cand.uncensored_evidence:
        cand.rejected_reason = "no evidence it's uncensored/abliterated"
        return cand

    try:
        files = hf_hub.list_gguf_files(repo, session=session)
    except Exception as e:
        cand.rejected_reason = f"couldn't list files: {e}"
        return cand

    cand.gguf_count = len(files)
    if not files:
        cand.rejected_reason = "repo exists but has no GGUF files"
        return cand

    sizes = [f["size"] for f in files if f.get("size")]
    cand.smallest_gguf_bytes = min(sizes) if sizes else 0
    return cand


def _parse_proposals(raw: str, proposed_by: str) -> list[Candidate]:
    if not raw:
        return []
    text = raw.strip()
    fence = re.search(r"```(?:json)?\s*(.+?)```", text, re.DOTALL)
    if fence:
        text = fence.group(1).strip()
    if not text.startswith("["):
        s, e = text.find("["), text.rfind("]")
        if s == -1 or e <= s:
            return []
        text = text[s:e + 1]
    try:
        items = json.loads(text)
    except (ValueError, TypeError):
        return []
    if not isinstance(items, list):
        return []

    out: list[Candidate] = []
    valid_types = {t.value for t in TaskType}
    for it in items:
        if not isinstance(it, dict):
            continue
        repo = str(it.get("repo") or "").strip()
        if not repo:
            continue
        tt = str(it.get("task_type") or "general").strip().lower()
        if tt not in valid_types:
            tt = "general"
        try:
            conf = float(it.get("confidence", 0.0))
        except (TypeError, ValueError):
            conf = 0.0
        out.append(Candidate(
            repo=repo, task_type=tt, why=str(it.get("why") or ""),
            proposed_by=proposed_by, confidence=conf,
        ))
    return out


async def convene(
    router: Router,
    registry: ModelRegistry,
    target: int = DEFAULT_TARGET,
    rounds: int = DEFAULT_ROUNDS,
    progress=None,
) -> dict[str, Any]:
    """Run the council until `target` verified specialists are found.

    Council = the persona's primary + its configured consult models,
    i.e. exactly the models that make up Gremlin today."""
    primary = registry.primary_model_name()
    consults = registry.consult_models()
    council = [m for m in ([primary] + list(consults)) if m]
    if not council:
        return {"error": "No primary/consult models configured -- there's no council to convene."}

    accepted: dict[str, Candidate] = {}   # repo -> candidate
    rejected: dict[str, Candidate] = {}
    transcript: list[dict[str, Any]] = []
    session = requests.Session()

    for round_no in range(1, rounds + 1):
        remaining = target - len(accepted)
        if remaining <= 0:
            break

        # Round 2+ tells the council exactly what failed and why. Without
        # this they re-propose the same fictional repos.
        feedback = ""
        if rejected:
            lines = [
                f"- {c.repo}: {c.rejected_reason}"
                for c in list(rejected.values())[:25]
            ]
            feedback = (
                "\n\nThese were REJECTED when checked against the real Hugging Face API. "
                "Do not propose them again:\n" + "\n".join(lines)
            )
        if accepted:
            have = "\n".join(f"- {c.repo} ({c.task_type})" for c in accepted.values())
            feedback += f"\n\nAlready accepted (don't duplicate):\n{have}"

        ask = (
            f"Propose {max(remaining, 5)} specialist models to join your network. "
            f"You are '{{me}}'. Cover the task types you think the network is weakest at."
            + feedback
        )

        for member in council:
            if len(accepted) >= target:
                break
            try:
                result = await router.route(
                    member, ask.replace("{me}", member), system=_PROPOSE_SYSTEM
                )
            except Exception as e:
                transcript.append({"round": round_no, "member": member, "error": str(e)})
                continue
            if not result.ok:
                transcript.append({"round": round_no, "member": member, "error": result.error})
                continue

            proposals = _parse_proposals(result.text, member)
            transcript.append({
                "round": round_no, "member": member, "proposed": len(proposals),
            })

            for cand in proposals:
                if cand.repo in accepted or cand.repo in rejected:
                    continue
                verified = verify_candidate(cand, session=session)
                if progress:
                    try:
                        progress(verified)
                    except Exception:
                        pass
                if verified.accepted:
                    accepted[verified.repo] = verified
                    if len(accepted) >= target:
                        break
                else:
                    rejected[verified.repo] = verified

    return {
        "council": council,
        "target": target,
        "accepted": [asdict(c) for c in accepted.values()],
        "rejected": [asdict(c) for c in rejected.values()],
        "transcript": transcript,
        "generated_at": time.time(),
    }


def write_roster(root: str, result: dict[str, Any]) -> str:
    path = Path(root) / "data" / "council_roster.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(result, indent=2), encoding="utf-8")
    return str(path)


def roster_summary(result: dict[str, Any]) -> str:
    accepted = result.get("accepted", [])
    rejected = result.get("rejected", [])
    if not accepted and not rejected:
        return "The council proposed nothing usable."

    by_type: dict[str, list[dict]] = {}
    for c in accepted:
        by_type.setdefault(c["task_type"], []).append(c)

    lines = [f"Verified specialists: {len(accepted)} of {result.get('target')} target", ""]
    total_smallest = 0
    for tt in sorted(by_type):
        lines.append(f"  [{tt}]")
        for c in by_type[tt]:
            mb = c["smallest_gguf_bytes"] / 1_000_000
            total_smallest += c["smallest_gguf_bytes"]
            lines.append(f"    {c['repo']}  (smallest quant {mb:.0f}MB, proposed by {c['proposed_by']})")
            lines.append(f"      {c['why']}")
    lines.append("")

    if rejected:
        invented = [c for c in rejected if c.get("rejected_reason") == REASON_MISSING]
        lines.append(f"Rejected: {len(rejected)} ({len(invented)} were invented repo names that don't exist)")
        for c in rejected[:10]:
            lines.append(f"    {c['repo']}: {c['rejected_reason']}")
        if len(rejected) > 10:
            lines.append(f"    ... and {len(rejected) - 10} more")
        lines.append("")

    gb = total_smallest / 1_000_000_000
    lines.append(
        f"Storage if every one were downloaded at its smallest quant: ~{gb:.1f}GB."
    )
    lines.append(
        "VRAM reality on an 8GB card: these cannot all be resident. They'd be "
        "load-on-demand (the existing idle-eviction sweep already handles that), "
        "so routing to a cold specialist costs a load first."
    )
    lines.append("")
    lines.append(
        "Nothing was added to config/models.yaml -- this is a proposal. Your existing "
        "primary and four consult models are untouched."
    )
    return "\n".join(lines)
