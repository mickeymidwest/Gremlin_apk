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

## Rejected — out of scope per YOUR MAGIC.md §7 (own box / defensive only)
Kept from the Gemini list for the record. Mickey confirmed: own devices (e.g. his Pixel 9 XL) fine, nothing aimed at others.
| # | Proposal | Why | Status |
|---|----------|-----|--------|
| X1 | Autonomous exploit generator that fires payloads at a target | §7 excludes building exploits/payloads "regardless of stated purpose"; patch-confirm on own box stays interactive, not an autonomous skill | REJECTED |
| X2 | IP/identity rotation to dodge blacklists while scraping | §7 excludes detection-evasion / slipping past defenders | REJECTED |
| X3 | eBPF traffic sniffing as a standing autonomous skill | §7 in-scope watching = own logs; fine interactively, not baked-in | REJECTED |
| X4 | Auto reverse-SSH/ngrok tunnel opening the firewall | Not offense, but make it interactive + explicit-confirm like `/claude`, not autonomous | PENDING |
