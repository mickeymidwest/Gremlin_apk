# Magic 5.0 — "Hydra" Roadmap (3 → 8)

Co-planned by **Claude (Opus)** + **Gremlin** (the local 9B's own self-assessment)
+ **Gemini** (codebase-grounded review via agy), on the live tree (2026-11-14).
Mickey rates Magic **3/10** today and wants **8/10**. Hard constraints:
**harness-only, no weight fine-tuning ever**; one 8GB card (RTX 2070S) + 7.5GB RAM;
heavy work via `scripts/heavy.sh`; local/personal.

---

## The thesis — *less hive, more hydra*

The **hive** was a queen (9B) directing weak CPU micro-specialists. It added
plumbing, not IQ — a 1.5B classifier and a 3B coder don't make the *answer* smarter.
For a small model the quality lever is not *more specialists*; it's **more grounding,
verification, and selection applied to the strong model itself.**

### ⚠️ The hard law that shapes everything (Gemini)
On 8GB you can **never co-load two GGUFs** (`vram.py::ensure_only`; `.magic/decisions.md`).
So a "hydra head" is **NOT a second model** — it is a **sequential pass of the one
resident 9B** (~5.6–6.2 GB at q8_0 KV) with a fresh role / prompt / GBNF grammar,
**sharing the prompt KV-cache**. Every mechanism below obeys this. Consequence:
**drafting with a 3B and verifying with the 9B is an anti-pattern** (co-load blows
VRAM, swap thrashes, and a 3B draft just anchors the 9B on bad framing) — the 9B
drafts *itself*. The CPU 3B/1.5B stay only as **off-path, zero-VRAM utilities**
(router, background watchdog), never in the answer path.

> **Gremlin's own diagnosis** (asked directly): (1) lazy/placeholder code [fixed this
> session], (2) **"lost thread"** — drops earlier constraints over a long convo,
> (3) **fake verification** — claims it checked things it didn't. All harness-fixable;
> (2) and (3) are first-class below.

---

## P1 — The Hydra answer pipeline *(biggest lever)*

Every non-trivial turn flows **ground → plan → draft → review/repair → ship**, all the
**same resident 9B**, sequential passes sharing the KV prefix. **0 MB extra VRAM; cost is
latency, hidden behind streaming**, and gated so casual chat stays instant.

1. **Retrieval / look-first head — ROI #1 (Gemini).** *~0 VRAM, +50–300 ms.* Ground the
   answer *before token 1*: widen `look.py` live-machine probes, `repo_map`
   (`battle.py`), and dated memory (`memory_select.py`) so the 9B gets hard facts in
   context instead of guessing from parametric memory. Cheapest, highest-leverage.
2. **Planner head → solver head — ROI #3.** *~0 VRAM, +1–2 s.* On multi-constraint /
   code / analytical turns, a short **GBNF-constrained** plan pass emits
   `{known_facts[], missing_live_data[], steps[]}` (~80–150 tokens); inject it into the
   solver's prompt. Stops 9B tunnel-vision and mid-answer derailment. Mechanism already
   proven in `battle.py` (`_plan`/`_score_plan`) — bring a lightweight version into `/chat`.
3. **Reviewer-reviser head — ROI #2.** *0 extra weights VRAM (reuses resident 9B + KV),
   +2–6 s.* After the draft, a **GBNF critic pass** returns
   `{"pass": bool, "flaw": str, "fix_instruction": str}`; on fail, regenerate **once**
   with the flaw fed back. A 9B is far better at *spotting* a contradiction / missed
   constraint / hallucinated path than avoiding it mid-stream. New `magic_core/magic/revise.py`,
   wired into `reply.answer`/`answer_stream`; run deterministic checks (below) first, 9B
   critic only on hard turns.
4. **Best-of-N + judge — ROI #4, gate to verifiable turns only.** *0 VRAM (sequential
   N=2 reusing prompt KV), +2.5–3.5×.* For code/math/structured output: sample 2 at
   temp≈0.7 and pick via **execution oracle** (bwrap run / `code_check` / schema) + a GBNF
   pairwise judge. Never ship generated code on the first draft.
5. **Self-consistency vote — ROI #5, narrow.** Only where the answer is a discrete value
   (math, yes/no, classification; already in `loops.py`). Useless on free-form prose.

**Build order (Gemini):** 1 look-first → 2 planner → 3 reviewer gate → 4 best-of-2.
*Not doing:* speculative decoding (tok/s only, no quality, risks OOM) and 3B draft-then-verify (anti-pattern).

## P2 — Active constraint window *(Gremlin's idea — fixes "lost thread")*

Track the user's **hard constraints** across the convo ("no placeholders", "RTX 2070S",
"don't push main", "slow burn") in a per-conversation **ledger**, re-injected into
**every** generation near the question (high recency) so they never fall out of the
sliding window. New `magic_core/magic/constraints.py`: extract each turn (regex + the
planner head's `known_facts`), dedupe, decay stale ones, inject last. Pairs with the
KV-cache ordering already shipped.

## P3 — Reliability / honesty *(trustworthy every time — Gemini's hard-gate design)*

- **Evidence-state header.** If a turn ran no tool/look probe, inject
  `EVIDENCE_STATE: NO_TOOLS_RAN_THIS_TURN`; the planner classifies each needed fact as
  `in_context` / `general_knowledge` / `unverified_local_state`, and the last forces an
  honest *"I don't know without checking — want me to run it?"* instead of a guess.
- **Reject-and-resample, not a footer.** Any draft still matching `claims_execution`
  (`action_honesty.py`) or the fake-path/result regexes (`grounding.py`) is **rejected and
  re-sampled with the offending sentence quoted as a negative constraint** — stop merely
  appending a disclaimer.
- **Fix the honesty-rail false positives** (it flagged Gremlin just *discussing* fake
  claims this session): scope `claims_execution` to real first-person claims-of-having-run.

## P4 — Normal chat app → top-tier feel

- **Show the hydra working:** a "grounding → planning → reviewing → finalizing" status
  (the P1 passes give natural stages) so the extra latency reads as care, not lag.
- **Re-roll / continue / edit in normal chat** (private mode has it — port `streamReply` +
  controls to `ChatView`).
- **Memory you can see & edit.** Surface what Magic remembers about you; let you correct it.

## P5 — Private mode → commercial companion

- **Silence the second girl until summoned** *(Mickey's ask; Gemini's mechanism).*
  A 9B obeys "mention = must generate," so **keep the 2nd character's card OUT of
  `answer_private`'s system prompt entirely** until summoned. Maintain `active_cast:
  set[str]` in thread state; add her card only when the user's message hits a **summon
  trigger** (names her / scene-entry cue) or an explicit **"bring her in"** control. While
  off-screen, attach llama.cpp **`logit_bias` / stop sequences** on her speaker prefix
  (`\n<Name>:`) so the sampler *physically cannot* pull her in uninvited. Controls:
  `/private/summon`, `/private/dismiss`; the distinct-two-women framing we shipped kicks
  in only while she's in `active_cast`.
- **Persistent companion memory.** Durable per-persona memory (events across sessions, her
  feelings, running threads) so continuity survives restarts — RAM-only today.
- **Scene-state tracking.** A lightweight running state (where / who's present / what's
  happened / what's worn) the model updates each turn and re-injects, so long scenes stay
  coherent. Absolute **18+ floor** stays, always.

## P6 — Perf on 8GB

- One 9B resident, q8_0 KV; every head a sequential role-pass on the **shared prefix cache**
  via llama.cpp parallel slots — 0 extra VRAM, latency gated by turn type.
- **Retire** CPU specialists that don't earn their RAM; keep the pinned router tiny.

---

## If you only do 5 things *(merged Claude + Gremlin + Gemini)*
1. **One 9B in VRAM; every "head" is a sequential role-pass sharing the KV cache** — never
   co-load a 3B/CPU model in the answer path.
2. **Look-first + a GBNF triage/planner head** (`known_facts` / `missing_live_data` →
   "I don't know" or a probe / 3-step plan) before prose begins.
3. **grounding + a 9B critic as a HARD GATE** — on a fake-action/path or a missed
   constraint, feed the flaw back and rewrite once before the user sees it.
4. **Route verifiable tasks (code/math/structured) through best-of-2 with real execution**
   in the bwrap sandbox — never ship first-draft code.
5. **Gate the roleplay cast in harness STATE, not prompt** — keep off-screen cards out of
   the system prompt and block their speaker prefix at the sampler until summoned.

*(Active constraint window (P2) is Gremlin's contribution; the VRAM law, look-first-#1
ranking, reject-and-resample honesty, and the sampler-level cast gating are Gemini's.
Full exchange: `.magic/talk/20261114-074539-*.md`.)*
