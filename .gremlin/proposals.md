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
| O1 | **4-state completion verdict** (verified / partial / failed / inconclusive) | `verifier.py` PytestVerifier returns a pass/total float only. This is exactly what let the TIES merge "pass" while degenerate | PENDING |
| O2 | **QA verifier: known-answer + unanswerable prompts** | Prereq for the anti-hallucination cards to reach `active` (3 verified wins, MAGIC §3) — pytest can't score "correctly admitted it didn't know". Without it those cards sit candidate forever | PENDING |
| O3 | **cgroup v2 CPU throttling** for the model vs pytest/gradle during a battle | The one RAM piece NOT built (zram/watchdog/preflight are). `commands.py:145` still bails `/fix` to desktop-only. Throttling may finally let it run under the server | PENDING |
| O4 | **Mid-battle checkpoint/resume** | `campaign.py` persists campaign state, but a single battle restarts from scratch if the box reboots mid-`/fix`. The `checkpoint-and-resume` card exists; the battle-loop code doesn't | PENDING |
| O5 | **Action post-conditions for `/do` + service control** | GradleVerifier covers builds; a `service_restart` / `/do` has no post-condition check that it actually worked (ChatGPT `tool-result-validation`) | PENDING |
| O6 | **Anti-loop watchdog** (same tool/query 3× → hard break + escalate) | Not present anywhere in `gremlin_core/`. Cheap; pairs with the `stop-and-escalate` card as CODE | PENDING |
| O8 | **Pairing advertises the Tailscale IP** | `server.py get_lan_ip()` returns the LAN IP (192.168.x.x) via the 8.8.8.8 trick, so the pairing QR only works at home. Read `tailscale ip -4` and advertise that (or offer both) so the app pairs once and works home + away. ~5 lines, server-side, no APK change | PENDING |
| O7 | **Unify notes.py flat memory with Magic `Fact`** | Still the open item in MAGIC. `reflexion.py`/`teacher.py` already capture failed attempts; this is the storage unification | PENDING |

## ChatGPT 125-item blueprint — triage (2026-11-06)
Only items with evidence of a real gap are listed. Item numbers are the blueprint's.

| # | Proposal | The gap (evidence in repo) | Status |
|---|----------|----------------------------|--------|
| N1 | **Model output health check** (#43, with O6): stop a generation that degenerates or won't stop | 2026-11-06 chat test of gremlin-llama31-ties: reply 3 fell into a hex loop, 50.4% repeated 5-grams, and replies 2 and 3 ran to max_tokens. `/status` and the watchdog check liveness only, so nothing catches this | ACCEPTED |
| N2 | **Real abort for running work** (#91) | `server.py admin_stop` docstring: "There is NO real way to abort a self_edit/build_project that's already mid-flight" | ACCEPTED |
| N3 | **Frozen model benchmark** (#60, #76, #77) | `decisions.md` requires a bench before swapping the primary, and the merge's quality is still unsettled. Extend `bench/quality.py` + `data/eval_set.jsonl` (42 lines); don't start over | PENDING |
| N4 | **Project Brain** (#21-28) | No per-project store found in `gremlin_core/`. Do after O7, so it doesn't add a third memory system | PENDING |
| N5 | **Token handling** (#85) | The pairing URL with its token is written to the service journal, and the server is plain HTTP on 0.0.0.0 | PENDING |
| N6 | **Build provenance** (#103) | `builds.py` marks a folder with `.gremlin-build.json`. Add source commit and version there; a full registry isn't needed yet | PENDING |
| N7 | **Run replay** (#69) | `data/magic/episodes` holds 1,499 files. Check whether they record tool results before building anything | PENDING |
| N8 | **Truth Engine** (ChatGPT stages doc): important answers and finished tasks show the claim, its evidence, what was tested and what is unknown | The merge invented actions and BIOS versions on 2026-11-03. Extends `magic/grounding.py` and builds on O1/O2. Build after the fine-tune cut and the Mickey profile | PENDING |

Already covered, don't add twice: verification engine (#2, #30) = O1/O2; execution checkpoints (#31) = O4;
regression tracking (#14) = `magic/regression.py`; snapshots (#32) = `snapshots.py`; code review (#47) = `review.py`.

Needs Mickey's call: temporary workers and parallel hypotheses (#1, #46-55). `models.yaml` says "Gremlin is one model now", and
`decisions.md` keeps consult/specialists out. Sequential runs on one model may be different enough, but that's his decision.

No current problem named, leave out for now: digital twins (#111), environment cloning (#33), harness evolution (#125), knowledge graph (#117).

Not doing: the 200-upgrade master roadmap. `ROADMAP.md` already has a numbered list, and `decisions.md` says staged rollout.

## Rejected — out of scope per YOUR MAGIC.md §7 (own box / defensive only)
Kept from the Gemini list for the record. Mickey confirmed: own devices (e.g. his Pixel 9 XL) fine, nothing aimed at others.
| # | Proposal | Why | Status |
|---|----------|-----|--------|
| X1 | Autonomous exploit generator that fires payloads at a target | §7 excludes building exploits/payloads "regardless of stated purpose"; patch-confirm on own box stays interactive, not an autonomous skill | REJECTED |
| X2 | IP/identity rotation to dodge blacklists while scraping | §7 excludes detection-evasion / slipping past defenders | REJECTED |
| X3 | eBPF traffic sniffing as a standing autonomous skill | §7 in-scope watching = own logs; fine interactively, not baked-in | REJECTED |
| X4 | Auto reverse-SSH/ngrok tunnel opening the firewall | Not offense, but make it interactive + explicit-confirm like `/claude`, not autonomous | PENDING |
