# Gremlin + Magic — what it does, for a walkthrough

**Gremlin** is a private AI agent running entirely on this desktop (no data
leaves the box except code to a private GitHub). **Magic** is the harness
around it — the tools, the skill library, and the loop that makes it get
better at real work over time.

The mental model: **Gremlin is the pilot, Magic is the machine it flies,
the skills are the loadout.** A better pilot (a stronger model) drops
into the same machine; a stronger machine (better skills/tools) helps any
pilot.

## The business case

Authorized bug-bounty / security research on Android apps and on local
businesses' systems. Gremlin does the repetitive craft — write the fuzz
harness, sweep the source for a vuln class, triage a crash, draft the
report — so a human spends their time on the findings that pay.

**Claude and ChatGPT are the senior reviewers.** They don't run the work
— Gremlin does, locally, cheaply. They review Gremlin's *code and
approach* and hand back concrete fixes. In one working session that
review loop roughly **tripled** Gremlin's success rate on real tasks.

## What works today

### 1. Finding bugs (fuzzing)
Give Gremlin a C parser and "write a fuzzer for it." It reads the source,
writes a libFuzzer harness, builds it with AddressSanitizer, and the
harness trips a real memory-corruption bug — a stack overflow, a heap
overwrite — in seconds.
- Practice targets: `~/Downloads/fuzz-practice{,-2,-3}` (a netstring
  parser, a binary TLV format, an INI reader — each with a planted bug).
- Run: `venv/bin/python scratch_fuzz_battle.py ~/Downloads/fuzz-practice`

### 2. Building an app from a spec
Give Gremlin a scaffolded Android app — the game-logic class stubbed out,
pinned by a JUnit test suite — and it fills in the implementation one
method at a time, compiling and testing each against the spec until the
suite is green.
- `method_builder.py` — the harness owns the file, the model only writes
  one short method body against that method's own spec + the exact tests
  that exercise it. No navigating, no guessing. When a body compiles but a
  test still fails, the harness feeds the exact JUnit assertion back and
  the model repairs it (it has caught its own inverted guards and missing
  braces this way).
- **Build-a-Lot (property-tycoon game): 0 → 13/13 tests, all 7 methods.**
  A complete, working game-logic class written by the local 7B.
- Targets: `~/Downloads/klondike` (solitaire), `~/Downloads/buildalot`
  (Build-a-Lot). Wired into the nightly loop as the path for scaffold
  builds.

### 3. Getting better on its own
`zoid_loop.py` runs Gremlin through a rotation of ~11 practice targets,
round after round. After every attempt it distils a lesson from a loss,
proposes new skill cards, and promotes the ones that keep helping. The
skill library grew from 30 hand-written cards to ~80 in a day.
- `deploy/zoid-nightly.sh` runs it overnight; the service is back up in
  the morning.

### 4. Talking to it
`/chat` on the phone (paired over Wi-Fi), token-streamed replies, its own
durable memory of facts you've told it, `/skill` to teach it a new
procedure that shapes its answers immediately.

## The security discipline (built in)

- **`scope-first`** is the first skill card: confirm written
  authorization / program scope before touching anything.
- Recon is read-only. No mass scanning, nothing outside a program's
  scope, no worm/C2 infrastructure.
- Fine-tuning never trains on offensive methodology.

## Where it needs to go

- The local 7B model is the ceiling on the hardest tasks (a from-scratch
  Android build). A one-time ~$3 cloud fine-tune (`deploy/CLOUD-FINETUNE.md`)
  or a better GPU lifts that.
- More real (authorized) targets in the rotation as the work comes in.
