"""Magic -- Gremlin's harness.

Gremlin is the model (the pilot). Magic is the frame it runs in: memory,
tools, the skill system, and the improvement loop. See MAGIC.md at the
repo root for the spec this package is built against.

Layout (built in order, see MAGIC.md section headings):
  types      -- plain dataclasses, JSON/YAML round-trip helpers
  store      -- YAML skill cards under data/skills/, JSON for the rest
  model      -- sync `complete()` adapter over gremlin_core.backends
  toolhost   -- shell + file tools with a path jail
  battle     -- one bounded attempt at a task (ReAct text protocol)
  reckoning  -- propose (new_skill / revise_skill / new_fact) -> gate
  lifecycle  -- candidate -> active -> deprecated
  verifier   -- the ONLY source of a battle outcome
  council    -- skill destination: hard-code into Gremlin vs keep in Magic
  campaign   -- the resurrect -> battle -> reckon -> audit loop
"""
