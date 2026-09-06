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

    @property
    def _deprecated_dir(self):
        d = self.skills_dir / "_deprecated"
        d.mkdir(parents=True, exist_ok=True)
        return d

    def _load_card(self, p: Path) -> Skill | None:
        raw = yaml.safe_load(p.read_text()) or {}
        rec = raw.pop("record", None) or {}
        skill = from_dict(Skill, raw)
        if skill is None:
            return None
        skill.record = from_dict(SkillRecord, rec) or SkillRecord()
        return skill

    def read_skills(self) -> list[Skill]:
        out: list[Skill] = []
        for p in sorted(self.skills_dir.glob("*.yaml")):
            s = self._load_card(p)
            if s:
                out.append(s)
        dep = self.skills_dir / "_deprecated"
        if dep.is_dir():
            for p in sorted(dep.glob("*.yaml")):
                s = self._load_card(p)
                if s:
                    out.append(s)
        return out

    def write_skills(self, skills: list[Skill]) -> None:
        """Full sync. Live cards (candidate/active) sit in data/skills/ one
        per name, hand-editable. Deprecated cards move to
        data/skills/_deprecated/<name>__<id8>.yaml so a name can hold both
        an old (retired) version and its revision."""
        live_keep, dep_keep = set(), set()
        for s in skills:
            if s.status == "deprecated":
                path = self._deprecated_dir / f"{slug(s.name)}__{s.id[-8:]}.yaml"
                dep_keep.add(path.name)
            else:
                path = self._skill_path(s.name)
                live_keep.add(path.name)
            self._write_skill_file(path, s)
        for p in self.skills_dir.glob("*.yaml"):
            if p.name not in live_keep:
                p.unlink()
        dep = self.skills_dir / "_deprecated"
        if dep.is_dir():
            for p in dep.glob("*.yaml"):
                if p.name not in dep_keep:
                    p.unlink()

    def _write_skill_file(self, path: Path, s: Skill) -> None:
        d = to_dict(s)
        # order the mapping so the card reads well when opened by hand
        ordered = {k: d[k] for k in (
            "id", "name", "status", "destination", "council_reviewed",
            "purpose", "trigger_when", "trigger_matcher", "procedure",
            "supersedes", "provenance", "created", "record",
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
