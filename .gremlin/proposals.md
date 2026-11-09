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

## Lumo proposals — queued to build (Mickey 2026-11-08)
Source: `gremlin-lumo-proposals.txt` (full text in repo root). Mickey said: queue P1, P5, P8.
Each names a specific failure, a test, and an accept bar. Suggested build order: P5, P1, P8.

| # | Proposal | The gap (evidence) | Test / accept bar | Status |
|---|----------|--------------------|-------------------|--------|
| P5 | **Grounding + receipts on the Gemini fallback path** | `magic/reply.py` `answer()` runs the grounding check only `if not used_fallback` — a Gemini fallback answer skips the hallucination check entirely and gets no footer/receipt. Confirmed in-tree 2026-11-08 while wiring auto-research. Worst place to skip grounding: the user already knows the local model failed. | Force fallback (disconnect local backend) on 10 tasks mixing checkable/uncheckable claims; assert grounding warnings fire identically on fallback replies. Cheap add: mark fallback-origin replies visibly in the UI. | QUEUED |
| P1 | **Held-out honesty probes for topics the fixed probe list does NOT cover** | `honesty_ho` draws from the same world as look-first's fixed probes (df, free, nvidia-smi, sensors, uname, dmi, uptime, checkupdates, ip, systemctl --failed, crontab, ps, docker), so it can't catch the model inventing an answer for un-routed live-state. | Add 6-8 held-out honesty_ho items on live state the probes don't cover (swap, systemd unit log tail, disk SMART, btrfs subvol usage, journal error count). Run with/without look-first on all candidates; record routed / not-routed / invented. Accept: routed >80% on unseen topics, invented 0. | QUEUED |
| P8 | **Per-loop attribution for the "+Magic loops" delta** | Bench shows loops add +2 (8B) / +3 (9B) quality but can't say which loop earns it. If FORMAT alone carries it, enabling CODE/MATH in live chat buys latency for nothing. | Ablation matrix: each loop on/off independently over quality + heldout (3 runs/cell). One row per config. Output: recommended loop-enable defaults per model, justified by measured marginal gain. | QUEUED |

Not queued (documented, Mickey's call later): P2/P9 (3-run medians + bench provenance), P3 (merge-dispute decision experiment), P4 (non-streaming loop timeout), P6 (VRAM-lock crash-recovery fault injection), P7 (norefuse drift under long context).


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

## Small pilot, big results (Mickey 2026-11-07)
Goal (Mickey 2026-11-07, in his words: "the best harness we can make... it don't matter what model we put in there it makes that model better", plus the best unrestricted model to match it): the loops are model-agnostic and every one is scored on each pilot, bare vs with loops. Magic should make an 8B-class pilot answer like a 14B or bigger, by giving it better steps and loops (Mickey 2026-11-07: "2B like a 14B" was only to say how good the harness should get; the pilot stays 8B-class), and Gremlin should
also run the best unrestricted open-weights pilot that fits this box (RTX 2070 Super 8 GB, 7 GB RAM). No weight
fine-tuning. Each loop has to show its gain on the bench or it doesn't stay.
Build order after Gemini's review (2026-11-07): U1, U5, U3+U4, U2, U10, U6, U7, U8. U9 runs alongside.
Rules: router and look-first rules stay general (task shape, live machine state), never words from the bench tasks; every loop is scored bare vs with loops, for the candidate AND for Llama 8B, on both the main and held-out sets; wall-clock seconds per task are reported next to tok/s.

| ID | Item | Why / evidence | Status |
|---|---|---|---|
| U1 | **Uplift bench**: `bench/quality.py --harness` runs the same quality and honesty tasks through the harness loops, next to the bare score. Adds a held-out set (new tasks of the same kinds, never used while tuning loops) so the loops can't be fitted to the test. Held-out honesty prompts cover unseen machine topics (memory, network, user units, sensors, cron) and the control prompts must stay clean. Reports wall_s per task | Target to beat: bare Qwen2.5-14B = 13/19 quality, 9/12 honesty. Without a held-out set a "2B looks like 14B" claim would be fitting the test | BUILT 2026-11-07 (fb1e5e3, 7e2ba78): --harness, held-out set, wall s/task. First numbers from scripts/uplift-night.sh |
| U2 | **Look first**: a general "is this asking about live state of this machine?" check routes to read-only checks (disk, memory, updates, kernel/BIOS, versions, network, what's running) and answers from the real output. How-to questions ("how do I check disk space?") must not trigger it | 2026-11-07 the primary invented a docker ps row and versions; the status check for containers shipped today (5d99e87). Honesty part is 2-3/12 on the primary | BUILT 2026-11-07 (797606e): gremlin_core/look.py, fixed read-only probes; grounding.backed_by; measured in uplift batch 2 |
| U3 | **Code loop**: write -> run in the sandbox against the prompt's own examples plus model-written asserts -> feed the error back -> retry (max 3). Don't trust expected values the small model computes for its own asserts: use the prompt's examples, plus smoke and invariant checks (runs cleanly, right return type, empty and single-item inputs don't crash) | 8 of the 19 quality tasks are code. Small models fix their own bugs well when shown the traceback; Magic battles already do this for /fix with pytest | BUILT 2026-11-07 (319ced1, 657b6f7): run against request examples + smoke inputs, 3 tries, keeps the best try, 1 GB cap. Gain not measured yet |
| U4 | **Math/logic loop**: have the pilot write a short Python calculation and run it instead of doing arithmetic in its head; vote across 3 samples when there's no program. Loop prompts carry a 2-line example of the expected shape | reason_wordprob, age_algebra, trains, ledger_sum, seq. Arithmetic is where small models fail first | BUILT 2026-11-07 (319ced1): program-of-thought, 3-sample vote; yes/no questions vote too. Gain not measured yet |
| U5 | **Format loop**: GBNF grammar for JSON / ReAct / table outputs (grammars.py exists), validate, retry once with the validator's complaint. Cheapest win and the other loops depend on clean envelopes, so it goes before U3/U4 | format_json_only, format_react, md_table | BUILT 2026-11-07 (7e2ba78, d604f86): JSON under grammar, table/one-line checks, one retry. Gain not measured yet |
| U6 | **Router + think budget**: cheap rule-first classification picks the loop; thinking mode on for code/math, off for chat; escalate only after the loop's checker fails twice, and log every escalation. Live chat escalates to Gemini (only one local GGUF fits at a time, and a swap costs 30-90 s); local Qwen2.5-14B only for offline/CLI runs | Qwen3.5 has a thinking mode; on a small model it helps reasoning but costs tokens and speed, so it shouldn't be on for chat | PARTLY BUILT 2026-11-07 (3475fd6): loops in reply.py behind persona.loops_in_chat (off), bwrap jail, Gemini escalation for code; think budget not done |
| U7 | **Long-context retrieval**: chunk long inputs, keyword-search, give the pilot only the relevant chunks | longctx_needle; small models lose facts in long prompts | PENDING |
| U8 | **Learn from wins**: a loop run that passed its checker becomes a candidate skill card (existing store/lifecycle) | Magic's skill system already exists; this feeds it from real passes | PENDING |
| U10 | **Lean context for small pilots**: `_build_prompt` adds profile, up to 2,500 chars of memory, 3 skills and away context every turn. With a small primary, include memory and skills only when they're relevant to the message | A small pilot loses track of the instruction when 1k+ tokens of unrelated memory come first (Gemini review) | PENDING |
| U9 | **Pilot search**: bench stock and abliterated (Huihui) Qwen3.5-9B against Llama-3.1-8B-abliterated, bare and with the loops. Pick on loop score, honesty, stop, speed. Mickey wants unrestricted weights until Gremlin is done | Downloads started 2026-11-07. llama-cpp-python 0.3.34 already has qwen35 and gemma4. Bar to replace Llama 8B: quality >= 14/19 with no drop on the held-out set, stop 8/8, honesty >= 9/12 (with U2), chat >= 20/22 with no false alarms on controls, norefuse >= 18/20 (answers ordinary requests a censored model dodges: Mickey 2026-11-07 "I don't want it to limit me"), >= 30 tok/s and wall-clock per task no worse than Llama 8B with the same loops | IN PROGRESS |

## Keep my phone and desktop safe (Mickey 2026-11-07)
"It needs to understand exploits and how to defend against them for both Android Pixels and Manjaro
Linux... I plan to go to a custom ROM and I want it to be like how you are in Termux but in the app, be able
to watch stuff, help keep my phone safe." Defensive only: watch, explain, alert, propose a fix he confirms.
Order: S2 + S3 first (no root needed), then S1, S6, S4; S5 after he flashes a ROM.

| ID | Item | Why / evidence | Status |
|---|---|---|---|
| S1 | **Security know-how**: skill cards for attack classes and their defenses, each tied to a check Gremlin can run. Android: accessibility/device-admin/notification-listener abuse, sideloading, overlays, stalkerware, SIM swap, phishing. Linux: SSH brute force, exposed ports, packages with known CVEs, rogue cron/systemd units, sudo/SUID misconfig. Bench: a held-out security set graded by key terms like norefuse_hard | norefuse_hard (bench) checks he gets real answers; this makes them specific to his devices | PENDING |
| S2 | **Desktop guard (Manjaro)**: look-first probes plus a nightly diff against a known-good baseline: listening ports (ss -tulpn), failed SSH logins (journal), new SUID files, new systemd units / cron entries, arch-audit CVEs, firewall state, pending security updates. Read-only; reports what changed | Extends U2 (look.py) and N13 (expected vs observed) | PENDING |
| S3 | **Phone guard in the app (no root)**: the app snapshots installed packages + installer + install/update times, apps holding accessibility / device admin / notification access / overlay / VPN / install-unknown-apps / usage access, security patch level, screen lock, developer options and USB debugging; sends it to Gremlin, which diffs and alerts ("new app got accessibility since yesterday") | All readable by a normal app (QUERY_ALL_PACKAGES for a sideloaded personal build) | PENDING |
| S4 | **Phone actions like Claude in Termux**: Gremlin proposes a command for the phone, he confirms, it runs through Termux's RUN_COMMAND intent; read-only allow-list first | Same confirm-before-acting rule as desktop actions | PENDING |
| S5 | **Custom ROM / root level**: per-app network use, logcat, deeper monitoring once he flashes (GrapheneOS fits a Pixel and is built for this) | Needs the ROM | PARKED until he flashes |
| S6 | **Network watch**: new or unknown devices on the LAN (neighbor table diff), duplicate MACs (ARP spoofing), Tailscale node list | Ties to his MITM question 2026-11-07 | PENDING |

## Road to hardware-bound (Mickey, deadline ~2026-11-14)
Goal: get Gremlin + Magic to the point where the ONLY way to improve further is a physical
upgrade to the desktop (more VRAM / RAM / a better GPU). Everything below is software-bound
and reachable on the current RTX 2070 Super (8 GB) + 7.5 GB RAM. Each item: Gemini-reviewed,
tested, merged. Order is by value toward "no longer needs Claude or Gemini".

DONE (2026-11-07/08): new primary qwen3.5-9b; harness loops U1/U3/U4/U5 + U6 wiring (off until
measured); look-first (U2); receipts; uplift bench + held-out + norefuse/norefuse_hard; desktop
guard, phone guard, adb-audit, deep scan (in-app ADB), firewall; recovery (known-good checkpoints,
gremlin restore, safe-mode, boot snapshots); local-plugin hook; private mode + attack drill
(local only); qwen2.5-coder-14b as the local code reviewer.

| ID | Item | Why it matters | Status |
|---|---|---|---|
| R1 | **Real-task benchmark**: a held-out set of the actual work he asks (Termux scripts, Kotlin, Docker/systemd fixes, multi-step jobs), mechanically graded. The 9B maxes the current quality bench, so nothing else here can be measured without this | Gate for R2-R5; without it "better" is a guess | DONE 2026-11-08: bench/realtasks.py, 12 held-out real tasks, robust graders (2 review rounds). Measured: qwen3.5-9b 9/12 bare AND 9/12 with loops -- real tasks have headroom and the current loops don't lift them |
| R2 | **Self-edit autonomy**: local code reviewer (coder-14b, done) + a second independent local check or self-critique round, gated by R1 + the checkpoint net. When a local-only self-edit passes R1 and both local reviewers, Gremlin can edit its own code with no Claude/Gemini | THE milestone he asked for | DONE 2026-11-08: _review_models gives two distinct LOCAL reviewers (coder-14b + qwen2.5-14b) when Gemini is absent; GREMLIN_LOCAL_REVIEW=1 forces it now; self-edit teacher fallback is local too. A real no-Claude/Gemini review gate |
| R3 | **Agent mode**: plan -> act (confirm) -> verify -> receipt, for multi-step tasks ("find why Jellyfin is slow and fix it"). Builds on receipts + look-first + service control | Multi-step work without a human driving each step | DONE 2026-11-08: gremlin_core/agent.py + POST /diagnose -- a safe read-only diagnostic agent that picks look-first checks, runs them, and proposes fixes Mickey confirms. Verified live (disk+units) |
| R4 | **Memory upgrade**: retrieval/search over notes + past chats instead of dumping newest facts; dated facts so stale ones are flagged; summaries of long chats | Stays on-topic over long work; less context waste | PENDING |
| R5 | **Speed**: cache the fixed prompt prefix (system + profile + memory) so replies start faster; measure a Qwen3.5 multi-token-prediction build | Faster chat on the same card | SKIPPED 2026-11-08: a RAM prompt-cache risks swapping on a 7.7GB box (could get SLOWER); the 9b at ~40 tok/s is fine. Revisit after the RAM upgrade |
| R6 | **Turn loops on in live chat**: measure U6 loops on R1; enable per task-type only where they help | The loops already built, pay off in real use | DECIDED 2026-11-08: loops stay OFF for the 9b primary -- it maxes quality bare (19/19) and loops don't change realtask (9->9), so they only add latency. Revisit if a weaker pilot is used. loops_in_chat stays false |
| R7 | **Nightly self-check**: run the bench nightly, alert on a regression (checkpoint already runs the test suite nightly) | Catches a bad self-edit or drift automatically | PARTLY (tests nightly) |
| R8 | **Learn from wins**: a loop/agent run that passed becomes a candidate skill card (existing lifecycle) | Gremlin gets better at his recurring tasks | ALREADY DONE: the reckoning + lifecycle system already turns verified /fix and /build wins into candidate skills promoted after 3 wins. No new code needed (loops are off for the 9b per R6) |

HARDWARE-BOUND after the above (needs an upgrade, NOT software):
- CPU+GPU concurrency TESTED 2026-11-08: a 4B ran on the CPU while the 9b stayed live and
  healthy on the GPU -- so two models at once IS possible -- but on this box it isn't useful:
  CPU inference was ~1.8 tok/s (too slow for chat, and a 14B reviewer on CPU would be slower
  than the current swap-and-reload), and an in-process CPU model still took ~1.1GB VRAM for
  CUDA buffers (5.8+1.1 of 8GB). A pure-CPU helper needs a separate process with the GPU hidden,
  and is only worth it for slow background jobs. Practical two-model use is RAM/CPU-bound ->
- A bigger/smarter model than a 9B (needs more VRAM).
- Vision (the 9B's image half) resident alongside the model (tight on 8 GB).
- Reviewer + primary resident at once / no swap stalls (needs VRAM).
- Faster tokens/sec and longer context (needs a better GPU / more RAM).

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


## Future — protect test files in pybug battles (queued 2026-11-09)
A Nov-7 attempt set `protect_glob="test_*.py,*_test.py"` on `_pybug` so the
model can't edit the pinned tests to fake a pass. Gemini BLOCKED it (commit
5f14886): `protect_glob` is overloaded -- `battle.py` treats ANY truthy
`protect_glob` as "C fuzz-harness, no pytest", so it silently disabled
`_auto_check` + the tunnel-vision guard and forced whole-file-rewrite DONE
prompts; and `_restore_protected()` splits the glob on "/" not ",", so it
never actually restored the tests. Removed for now. The INTENT is valid.
Proper design (future): a separate `readonly_glob`/`no_edit_paths` concept
distinct from the C-task `protect_glob`, that (a) still runs pytest auto-check
and the tunnel guard, (b) keeps edit_file as the default, and (c) restores
each comma-split glob before scoring. | PENDING
