"""Magic's store: YAML skill cards under data/skills/, JSON for the rest.

    <root>/
      data/skills/<name>.yaml       one skill card each -- human-editable,
                                    the portable unit (MAGIC.md section 3)
      gremlin_memory.txt (repo parent)  semantic memory -- hand-editable
      data/magic/campaign.json      CampaignState
      data/magic/episodes/<id>.json one BattleResult each   episodic memory
      data/magic/work/              per-battle working copies of a target repo

Skills are YAML on purpose: mickey edits them by hand, diffs them, reverts
them. Everything else is machine-written churn, so it stays JSON.
"""
from __future__ import annotations

import hashlib
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


def _hashid(text: str) -> str:
    return hashlib.sha1(text.encode()).hexdigest()[:8]


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
        # unique tmp: a shared ".tmp" sibling can be half-written by one
        # writer and .replace()'d into place by another (server threads).
        tmp = path.with_suffix(path.suffix + f".{os.getpid()}-{_hashid(str(id(data)))}.tmp")
        try:
            tmp.write_text(json.dumps(data, indent=2))
            tmp.replace(path)  # atomic on POSIX
        finally:
            tmp.unlink(missing_ok=True)

    # -- skills (procedural memory, YAML) -----------------------------

    def _skill_path(self, name: str) -> Path:
        return self.skills_dir / f"{slug(name)}.yaml"

    @property
    def _deprecated_dir(self):
        d = self.skills_dir / "_deprecated"
        d.mkdir(parents=True, exist_ok=True)
        return d

    def _load_card(self, p: Path) -> Skill | None:
        # these cards are hand-edited on purpose -- a YAML typo in one
        # must not blind Magic to every other skill.
        try:
            raw = yaml.safe_load(p.read_text()) or {}
        except (yaml.YAMLError, OSError) as e:
            print(f"[store] skipping unreadable skill card {p.name}: {e}")
            return None
        if not isinstance(raw, dict):
            print(f"[store] skipping skill card {p.name}: not a mapping")
            return None
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
        tmp = path.with_suffix(f".{os.getpid()}.yaml.tmp")
        try:
            tmp.write_text(yaml.safe_dump(ordered, sort_keys=False, width=88))
            tmp.replace(path)
        finally:
            tmp.unlink(missing_ok=True)

    # -- facts (semantic memory) -----------------------------------
    #
    # One store, hand-editable: gremlin_memory.txt (the same file "remember
    # that X" writes and reply.py folds into every chat prompt). A fact
    # learned in a battle and a fact mickey told Gremlin now live in the
    # same place. Lines look like:  - [learned] the fact  <!--fact_ab12-->
    # and a plain "- text" line (hand-added) parses fine too.

    # strip a leading "- ", any number of "[tag]" prefixes (timestamp,
    # [user], [learned]...), and a trailing "<!--id-->"
    _FACT_RE = re.compile(r"^\s*-\s*(?:\[[^\]]*\]\s*)*(.*?)\s*(?:<!--\s*(\S+)\s*-->)?\s*$")

    def _memory_path(self) -> Path:
        from ..notes import memory_file_path
        return Path(memory_file_path(str(self.root)))

    def read_facts(self) -> list[Fact]:
        p = self._memory_path()
        if not p.exists():
            return []
        out: list[Fact] = []
        for line in p.read_text().splitlines():
            s = line.strip()
            if not s or s.startswith("#") or not s.startswith("-"):
                continue
            m = self._FACT_RE.match(s)
            text = (m.group(1) if m else s.lstrip("- ")).strip()
            if not text:
                continue
            fid = (m.group(2) if m and m.group(2) else "fact_" + _hashid(text))
            out.append(Fact(id=fid, text=text))
        return out

    def write_facts(self, facts: list[Fact]) -> None:
        """Append any fact not already recorded. Leaves the user's header
        and hand-written notes untouched."""
        from ..notes import remember_fact
        have = {f.text for f in self.read_facts()}
        for f in facts:
            if f.text not in have:
                remember_fact(str(self.root), f"[learned] {f.text}  <!--{f.id}-->")
                have.add(f.text)

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
