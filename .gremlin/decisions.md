# Decisions log: settled, do not revisit

Anything here was decided by Mickey (often after testing). Claude MUST check this file before
changing how ANY part of the project works. If a change would go against an entry, don't make it:
ask Mickey. Only Mickey adds, changes or removes entries.

Format per entry:
## <area>
- USE: <the approach we're going with> (why)
- NOT: <the approach not to drift back to> (why)
- If results are weak: improve the USE approach. Don't switch to NOT.

Entries marked (MAGIC.md) were copied from the spec: already tested and documented there.
Mickey: delete any that aren't really settled.

---

## Primary model (MAGIC.md §1)
- USE: llama-3.1-8b-abliterated Q4_K_M as primary (config/models.yaml, restored 2026-11-03), Gemini fallback.
  qwen2.5-14b is the other registered local model. The Qwen 7B models are no longer on disk.
- NOT: swapping the primary without re-running the bench.

## KV cache (MAGIC.md §1, models.yaml, confirmed 2026-11-06)
- USE: kv_cache_type q8_0 for every local GGUF unless VRAM forces otherwise.
- NOT: q4_0 KV. Tested: it causes pure token-repetition / degeneration (Qwen2.5 confirmed 2026-09-12;
  on Llama 3.1, the 2026-11-06 A/B found no KV effect: gremlin-llama31-ties ran away on both q4_0 and q8_0,
  and the unmerged primary was clean on both. The fault there is in the merge, not the KV quant).
  Exception on record: the 14B entry needs q4_0 at 8k or it CUDA-OOMs — a VRAM tradeoff, accepted there only.

## GPU offload / VRAM (MAGIC.md §1, build log)
- USE: n_gpu_layers -1 (full offload) for the primary; never two local GGUFs resident (vram.py ensure_only).
- NOT: partial offload of the primary; loading a second model alongside.

## /fix and /build (MAGIC.md open list)
- USE: desktop CLI only (`gremlin magic fix …`); the app gets the "run it on the desktop" message.
- NOT: running battles under the live server. 7.5GB RAM swap-thrashes and the watchdog restarts it mid-run.

## Phone app (MAGIC.md §5)
- USE: thin client: chat + download desktop builds. That's it.
- NOT: running models, builds or the harness loop on the phone.

## Old machinery (MAGIC.md 5b)
- USE: Magic's loop (battle → verify → reckon → gate).
- NOT: bringing back consult.py / council.py (old) / specialists.py / bench.py / distill.py. Deleted on purpose.

## Battle outcomes (MAGIC.md §3)
- USE: outcome comes ONLY from a Verifier (PytestVerifier, FuzzVerifier, …).
- NOT: accepting a change on the model's own "simulation" or claims.

## Edits (MAGIC.md §8)
- USE: edit_file search/replace with precheck; phase-gated tools; repo_map.
- NOT: whole-file rewrites as the default.

## Security boundaries (MAGIC.md §7, §7a)
- USE: boundaries enforced in code (robofuse-stack refusal in run_command, allow-listed service_control,
  capability tags, path jail). Infra defense = defensive, own box only.
- NOT: relying on prompt/tool-description text alone (confirmed not enough), or extending/controlling robofuse-stack.

## Model merges (confirmed from repo 2026-11-06)
- USE: TIES, same architecture ONLY, via deploy/merge-local-llama31.py (it refuses non-Llama-3.1 input).
  Valid set on this box: Meta-Llama-3.1-8B-abliterated (base) + Hermes-3-Llama-3.1-8B. Merge HF safetensors,
  then convert+quantize to GGUF. A/B the result against the current primary; keep only if it wins.
- NOT: merging across architectures (e.g. Qwen2.5-Coder into a Llama base) — tensors don't line up,
  --allow-crimes doesn't fix it, the result repeats/degenerates. Not real mergekit flags
  (MERGEKIT_USE_CPU / LOW_CPU_MEM_USAGE). Not GGUF inputs (mergekit needs full-weight HF repos).

## Fine-tuning (confirmed from repo 2026-11-06)
- USE: the QLoRA pipeline is proven safe on this box (local Unsloth, GPU-only, no CPU offload). The real
  payoff run is the 7B on a rented GPU (deploy/CLOUD-FINETUNE.md), and only after data/conversations/
  has more real back-and-forth usage to train on.
- NOT: registering a fine-tune without A/B-ing it against the plain base first. Every attempt so far
  (3B QLoRA 2026-09-07; local Unsloth 7B/8B 2026-09-13) came out WORSE than the base — repetition loops,
  misrouting plain requests into build offers, confabulated "previous convo" notes — and was unregistered.
  Likely cause: too few examples, too heavy on scaffolding/battle transcripts vs real conversation.
- If results are weak: fix the DATA (more real conversation), don't just rerun. Log every run in .gremlin/runs.md.

## Outside upgrades (ChatGPT review, Nov 2026)
- USE: staged rollout per .gremlin/proposals.md: verification-as-code first (Batch 1).
- NOT: dumping 100 skill cards in at once, or rejecting proposals without evidence (CALIBRATION.md §9).
