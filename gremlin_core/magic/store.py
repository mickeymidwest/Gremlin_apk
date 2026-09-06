"""Magic's store: YAML skill cards under data/skills/, JSON for the rest.

    <root>/
      data/skills/<name>.yaml       one skill card each -- human-editable,
                                    the portable unit (MAGIC.md section 3)
      data/magic/memory.json        [Fact, ...]        semantic memory
      data/magic/campaign.json      CampaignState
      data/magic/episodes/<id>.json one BattleResult each   episodic memory
      data/magic/work/              per-battle working copies of a target repo

Skills are YAML on purpose: mickey edits them by hand, diffs them, reverts
them. Everything else is machine-written churn, so it stays JSON.
"""
from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Optional

import yaml

from .types import (
    BattleResult, CampaignState, Fact, Skill, SkillRecord,
    to_dict, from_dict,
)

_SLUG_RE = re.compile(r"[^a-z0-9]+")


def slug(s: str) -> str:
    return _SLUG_RE.sub("-", s.lower()).strip("-") or "unnamed-skill"


class Store:
    def __init__(self, root: str | os.PathLike):
        self.root = Path(root)
        self.skills_dir = self.root / "data" / "skills"
        self.magic_dir = self.root / "data" / "magic"
        self.episodes_dir = self.magic_dir / "episodes"
        self.work_dir = self.magic_dir / "work"
        for d in (self.skills_dir, self.episodes_dir, self.work_dir):
            d.mkdir(parents=True, exist_ok=True)

    # -- low-level json -------------------------------------------------

    def _read_json(self, path: Path, default):
        if not path.exists():
            return default
        return json.loads(path.read_text())

    def _write_json(self, path: Path, data) -> None:
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(json.dumps(data, indent=2))
        tmp.replace(path)  # atomic on POSIX

    # -- skills (procedural memory, YAML) -----------------------------

    def _skill_path(self, name: str) -> Path:
        return self.skills_dir / f"{slug(name)}.yaml"

    def read_skills(self) -> list[Skill]:
        out: list[Skill] = []
        for p in sorted(self.skills_dir.glob("*.yaml")):
            raw = yaml.safe_load(p.read_text()) or {}
            rec = raw.pop("record", None) or {}
            skill = from_dict(Skill, raw)
            if skill is None:
                continue
            skill.record = from_dict(SkillRecord, rec) or SkillRecord()
            out.append(skill)
        return out

    def write_skills(self, skills: list[Skill]) -> None:
        """Full sync: write every skill, delete YAML files with no
        matching skill left (e.g. a rename)."""
        keep = set()
        for s in skills:
            path = self._skill_path(s.name)
            # if two skills share a name (an old + its supersessor) keep
            # the newest -- lifecycle deprecates the old one, but on disk
            # one file per name; the deprecated copy lives in its history.
            keep.add(path.name)
            self._write_skill_file(path, s)
        for p in self.skills_dir.glob("*.yaml"):
            if p.name not in keep:
                p.unlink()

    def _write_skill_file(self, path: Path, s: Skill) -> None:
        d = to_dict(s)
        # order the mapping so the card reads well when opened by hand
        ordered = {k: d[k] for k in (
            "id", "name", "status", "purpose", "trigger_when",
            "trigger_matcher", "procedure", "supersedes", "provenance",
            "created", "record",
        ) if k in d}
        tmp = path.with_suffix(".yaml.tmp")
        tmp.write_text(yaml.safe_dump(ordered, sort_keys=False, width=88))
        tmp.replace(path)

    # -- facts (semantic memory) -------------------------------------

    def read_facts(self) -> list[Fact]:
        return [from_dict(Fact, f) for f in
                self._read_json(self.magic_dir / "memory.json", [])]

    def write_facts(self, facts: list[Fact]) -> None:
        self.magic_dir.mkdir(parents=True, exist_ok=True)
        self._write_json(self.magic_dir / "memory.json", [to_dict(f) for f in facts])

    # -- episodes (episodic memory) --------------------------------

    def append_episode(self, result: BattleResult) -> None:
        path = self.episodes_dir / f"{result.battle_id}.json"
        path.write_text(json.dumps(to_dict(result), indent=2))

    def read_episodes(self, limit: Optional[int] = None) -> list[BattleResult]:
        paths = sorted(self.episodes_dir.glob("*.json"))
        results = [from_dict(BattleResult, json.loads(p.read_text())) for p in paths]
        return results[-limit:] if limit else results

    # -- campaign state -------------------------------------------

    def get_state(self) -> CampaignState:
        return (from_dict(CampaignState, self._read_json(self.magic_dir / "campaign.json", {}))
                or CampaignState())

    def set_state(self, state: CampaignState) -> None:
        self.magic_dir.mkdir(parents=True, exist_ok=True)
        self._write_json(self.magic_dir / "campaign.json", to_dict(state))

    # -- battle working dir --------------------------------------

    def battle_workdir(self, battle_id: str) -> Path:
        d = self.work_dir / battle_id
        d.mkdir(parents=True, exist_ok=True)
        return d
