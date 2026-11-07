# Proposals

Reconciled against the live repo (mickeymidwest/gremlin_apk @ 0597293, read 2026-11-06).
The uploaded MAGIC.md was stale; this reflects what's actually in the tree.
Claude processes these per CALIBRATION.md §9: default YES for a real fix, reject only with
evidence, Gemini is tiebreaker, Mickey's call is final.

## Already shipped — do NOT re-propose
- Grammar/GBNF constraints on output — `gremlin_core/backends/grammars.py` (was "P14")
- ~230 skill cards incl. anti-hallucination + reasoning + security sets ChatGPT/Gemini suggested
  (epistemic-reasoning, stop-and-escalate, citation-integrity, prompt-injection-resistance, …)
- GradleVerifier — `gremlin_core/magic/gradle_verifier.py` (so `/build android` is verified)
- Maker-checker review — `gremlin_core/review.py` (APPROVE / REQUEST_CHANGES)
- RAM relief — `deploy/setup-zram.sh`, `deploy/ram_watchdog.py`, `deploy/gremlin-preflight.sh`, zram configs
- diff-only edit_file + plan/reasoning-sandwich — MAGIC §8 (already in the loop)
- Same-arch merge tool that refuses cross-arch — `deploy/merge-local-llama31.py`

## Still open — ranked, each a real gap in the current tree
| # | Proposal | The gap (evidence in repo) | Status |
|---|----------|----------------------------|--------|
| O1 | **4-state completion verdict** (verified / partial / failed / inconclusive) | `verifier.py` PytestVerifier returns a pass/total float only. This is exactly what let the TIES merge "pass" while degenerate | PARTLY DONE |
| O2 | **QA verifier: known-answer + unanswerable prompts** | Prereq for the anti-hallucination cards to reach `active` (3 verified wins, MAGIC §3) — pytest can't score "correctly admitted it didn't know". Without it those cards sit candidate forever | PENDING |
| O3 | **cgroup v2 CPU throttling** for the model vs pytest/gradle during a battle | The one RAM piece NOT built (zram/watchdog/preflight are). `commands.py:145` still bails `/fix` to desktop-only. Throttling may finally let it run under the server | PENDING |
| O4 | **Mid-battle checkpoint/resume** | `campaign.py` persists campaign state, but a single battle restarts from scratch if the box reboots mid-`/fix`. The `checkpoint-and-resume` card exists; the battle-loop code doesn't | PENDING |
| O5 | **Action post-conditions for `/do` + service control** | GradleVerifier covers builds; a `service_restart` / `/do` has no post-condition check that it actually worked (ChatGPT `tool-result-validation`) | PENDING |
| O6 | **Anti-loop watchdog** (same tool/query 3× → hard break + escalate) | Not present anywhere in `gremlin_core/`. Cheap; pairs with the `stop-and-escalate` card as CODE | DONE |
| O8 | **Pairing advertises the Tailscale IP** | `server.py get_lan_ip()` returns the LAN IP (192.168.x.x) via the 8.8.8.8 trick, so the pairing QR only works at home. Read `tailscale ip -4` and advertise that (or offer both) so the app pairs once and works home + away. ~5 lines, server-side, no APK change | DONE 2026-11-07: pairing uses GREMLIN_PAIR_HOST, else Tailscale, else LAN; `gremlin pair` shows the QR |
| O7 | **Unify notes.py flat memory with Magic `Fact`** | Still the open item in MAGIC. `reflexion.py`/`teacher.py` already capture failed attempts; this is the storage unification | MOSTLY DONE |

## ChatGPT 125-item blueprint — triage (2026-11-06)
Only items with evidence of a real gap are listed. Item numbers are the blueprint's.

| # | Proposal | The gap (evidence in repo) | Status |
|---|----------|----------------------------|--------|
| N1 | **Model output health check** (#43, with O6): stop a generation that degenerates or won't stop | 2026-11-06 chat test of gremlin-llama31-ties: reply 3 fell into a hex loop, 50.4% repeated 5-grams, and replies 2 and 3 ran to max_tokens. `/status` and the watchdog check liveness only, so nothing catches this | ACCEPTED |
| N2 | **Real abort for running work** (#91) | `server.py admin_stop` docstring: "There is NO real way to abort a self_edit/build_project that's already mid-flight" | ACCEPTED |
| N3 | **Frozen model benchmark** (#60, #76, #77) | `decisions.md` requires a bench before swapping the primary, and the merge's quality is still unsettled. Extend `bench/quality.py`; `data/eval_set.jsonl` is old fine-tune output, not a benchmark | DONE 2026-11-07: quality, stop, honesty and chat parts, suite hash, `--compare`. First run (bare model, Gremlin's prompt): primary 13/19 · 8/8 · 3/12, merge v2 13/19 · 1/8 · 2/12, abliterated 14B 13/19 · 8/8 · 5/12 at 24 tok/s, old 14B 13/19 · 8/8 · 9/12 at 24 tok/s. Primary stays |
| N4 | **Project Brain** (#21-28) | No per-project store found in `gremlin_core/`. Do after O7, so it doesn't add a third memory system | PENDING |
| N5 | **Token handling** (#85) | The pairing URL with its token is written to the service journal, and the server is plain HTTP on 0.0.0.0 | PARTLY DONE 2026-11-07: token no longer printed (the journal had 570 copies), request logs redact token=, threads.json keeps a hash. Old journal entries still hold it; plain HTTP is unchanged (Tailscale encrypts away from home) |
| N6 | **Build provenance** (#103) | `builds.py` marks a folder with `.gremlin-build.json`. Add source commit and version there; a full registry isn't needed yet | DONE 2026-11-07: marker records source commit, branch, dirty flag and APK version; /builds returns them |
| N7 | **Run replay** (#69) | `data/magic/episodes` holds 1,499 files. Check whether they record tool results before building anything | PARTLY DONE |
| N8 | **Truth Engine** (ChatGPT stages doc): important answers and finished tasks show the claim, its evidence, what was tested and what is unknown | The merge invented actions and BIOS versions on 2026-11-03. Extends `magic/grounding.py` and builds on O1/O2. Build after the fine-tune cut and the Mickey profile | STEP 1 DONE 2026-11-07: grounding flags made-up results in chat (restart worked, scan results, disk numbers, update counts, pretend `*runs command*`). Bench chat part on the primary: 6 of 12 trap replies made something up and all 6 got the warning, 10 of 10 ordinary replies had no false alarm. Next: claim / evidence / tested / unknown for finished tasks |

Already covered, don't add twice: verification engine (#2, #30) = O1/O2; execution checkpoints (#31) = O4;
regression tracking (#14) = `magic/regression.py`; snapshots (#32) = `snapshots.py`; code review (#47) = `review.py`.

Needs Mickey's call: temporary workers and parallel hypotheses (#1, #46-55). `models.yaml` says "Gremlin is one model now", and
`decisions.md` keeps consult/specialists out. Sequential runs on one model may be different enough, but that's his decision.

Moved to the hardware queue below: digital twins (#111), environment cloning (#33), harness evolution (#125), knowledge graph (#117).

Waiting on Gemini: the rest of the 125 items and the 200-upgrade master roadmap. Claude passed on these without review, which `decisions.md` no longer allows.

## ChatGPT plan and 15 ideas: after Gemini's review (2026-11-07)
Gemini checked these against the repo; Claude verified its "already exists" claims for O6, O7, N7 and verdict.py.

| # | Proposal | Where it landed | Status |
|---|----------|-----------------|--------|
| N9 | **Pre-flight check** before battles and builds (disk and RAM) | Parts exist: `gremlin-preflight.sh`, `heavy.sh`, `vram.py` | PENDING |
| N10 | **Contradiction Hunter** as a script with no model calls | Real drift found between docs and config | PENDING |
| N11 | **Skill Collision Detector** with no automatic deletes | `lifecycle.py` already retires failing and stale cards | PENDING |
| N12 | **Weekly self-audit report**, no automatic changes | Extends `defense.py` and `opportunities.py` | PENDING |
| N13 | **Expected vs observed state** snapshots, no always-on daemon | Extends `/status`, `defense.py` | PENDING |
| N14 | **Fact dates** for flagging old facts | Keep date tags in `store.py` | PENDING |
| N15 | **Model and fact details in episodes, secrets scrubbed from mutation_log** | Finishes N7 | PENDING |
| H7 | **Mission Planner** | After O3, O4 and N4 | PENDING |

Merged, not new: lie detector into N8/O5; reality check into O2/O5; recovery into O4/N2; goal tracker into N4; Mission Control into A1.
Already exists: personality separation (`persona.py`); the Constitution (CALIBRATION.md, decisions.md, MAGIC.md).
Claude and Gemini disagree with ChatGPT: a central policy engine (Gemini: duplicates tools.py, service_control.py and mutation_log.py) and a Model Laboratory with more merge sweeps (Gemini: negative value for now). Mickey decides.

## App redesign (requested by Mickey 2026-11-07)
| # | Proposal | Scope | Status |
|---|----------|-------|--------|
| A1 | **Polished Gremlin app** | New look, better chat screen, reorganized Settings, nicer Builds, Stop button, live status, model switcher, memory and skills views, profile editor once built | PARTLY DONE: 1.3 redesign, 1.4 profile editor. Left: Settings sections, Builds redesign |

## Queued for after hardware upgrades (parked 2026-11-07, not rejected)
Bring these back up when the upgrade in the last column happens. The unlocks are Claude's estimates until Gemini has reviewed them.

| # | Proposal | Blueprint items | Unlocked by | Status |
|---|----------|-----------------|-------------|--------|
| H1 | Temporary workers and parallel hypotheses | #1, #46-55, stage 6 | More VRAM, or sequential on one model if Mickey OKs it | PENDING |
| H2 | Digital twin and environment cloning | #33, #111, stage 7 | More RAM and an SSD | PENDING |
| H3 | Harness evolution experiments | #125, stage 8 | N3 benchmark first, then more RAM and an SSD | PENDING |
| H4 | Knowledge graph | #117, stage 9 | Not hardware: after N4 and O7 | PENDING |
| H5 | Research frontiers | stages 11-15 | Better GPU and more RAM | PENDING |
| H6 | Network and family protection | MAGIC.md §7 | After Mickey's move | PENDING |

## Rejected — out of scope per YOUR MAGIC.md §7 (own box / defensive only)
Kept from the Gemini list for the record. Mickey confirmed: own devices (e.g. his Pixel 9 XL) fine, nothing aimed at others.
| # | Proposal | Why | Status |
|---|----------|-----|--------|
| X1 | Autonomous exploit generator that fires payloads at a target | §7 excludes building exploits/payloads "regardless of stated purpose"; patch-confirm on own box stays interactive, not an autonomous skill | REJECTED |
| X2 | IP/identity rotation to dodge blacklists while scraping | §7 excludes detection-evasion / slipping past defenders | REJECTED |
| X3 | eBPF traffic sniffing as a standing autonomous skill | §7 in-scope watching = own logs; fine interactively, not baked-in | REJECTED |
| X4 | Auto reverse-SSH/ngrok tunnel opening the firewall | Not offense, but make it interactive + explicit-confirm like `/claude`, not autonomous | PENDING |
