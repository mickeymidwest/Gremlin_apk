"""The Hydra -- Magic's multi-head model roster + router (Magic 3.0 "Hydra").

Magic is the body; each HEAD is a model with a specialty -- the general
assistant, the uncensored roleplay voice, a deep reasoner, a heavy coder, the
tiny CPU classifier. The router picks the right head for a task.

A head is only AVAILABLE when it fits the hardware *right now* (VRAM for GPU
heads, free host RAM for CPU-lane / expert-offloaded heads). So a head too big
for today -- the MoE "deep" brain -- sits registered but DORMANT and lights up on
its own when the RAM/VRAM grows. No code change to grow Magic's brain; just
hardware. On an 8GB card only one GPU head is resident at a time
(vram.ensure_only), so the router is swap-cost-aware: it stays on the resident
head unless a task clearly pays for the swap. With more VRAM, heads co-reside and
routing becomes free.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

# task kind -> which head.kind serves it best (first match wins), always
# falling back to "general".
KIND_FOR_TASK = {
    "chat": ["general"],
    "private": ["uncensored", "general"],
    "code": ["code", "draft", "general"],
    "reason": ["reason", "general"],
    "review": ["reason", "general"],
    "classify": ["classify", "general"],
    "deep": ["deep", "reason", "general"],
}


import re as _re

_CODE_RE = _re.compile(
    r"(?i)```|\b(code|coding|script|function|func|method|class|bug|debug|error|traceback|"
    r"regex|refactor|compile|syntax|sql|query|bash|shell|python|javascript|typescript|kotlin|"
    r"rust|golang|\bgo\b|java|c\+\+|html|css|yaml|json|api|endpoint|docker|systemd)\b")
_REASON_RE = _re.compile(
    r"(?i)\b(why|how (does|do|would|can|should)|explain|analyze|analyse|compare|trade-?off|"
    r"prove|derive|design|architect|reason|think (through|about)|plan|strategy|implications|"
    r"pros and cons|step[-\s]by[-\s]step|walk me through|break (it|this) down)\b")
_DEEP_RE = _re.compile(
    r"(?i)\b(think (hard|deeply|carefully)|work (it|this) out|hard problem|complex|complicated|"
    r"thoroughly|in depth|deep dive|from first principles|rigorous|prove that|derive)\b")


def classify_task(message: str, private: bool = False) -> str:
    """Map a request to a task kind the router understands: private | code |
    deep | reason | chat. Cheap + deterministic (no model call). 'deep' is for
    genuinely hard / open-ended reasoning that earns the big head."""
    if private:
        return "private"
    m = (message or "").strip()
    if not m:
        return "chat"
    if _CODE_RE.search(m):
        return "code"
    if _DEEP_RE.search(m):
        return "deep"
    hard = _REASON_RE.search(m)
    if hard and len(m.split()) >= 25:
        return "deep"      # a long, reasoning-heavy ask -> the deep brain
    if hard:
        return "reason"
    return "chat"


@dataclass
class Head:
    name: str                 # the model/backend registry key
    specialty: str            # one-line human description
    kind: str                 # general|uncensored|reason|code|draft|classify|deep
    gpu: bool = True          # loads into VRAM (vs the CPU lane)
    footprint_mb: int = 0     # VRAM it needs resident (GPU heads)
    requires_ram_mb: int = 0  # free host RAM needed to run it (CPU / MoE offload)
    priority: int = 50        # tie-breaker; higher = preferred for its kind
    enabled: bool = True      # a disabled head is registered but never routed to
    note: str = ""


def _free_ram_mb() -> float:
    try:
        with open("/proc/meminfo") as f:
            for line in f:
                if line.startswith("MemAvailable:"):
                    return int(line.split()[1]) // 1024
    except Exception:
        pass
    return 0.0


def default_heads(free_ram_mb: Optional[float] = None) -> list[Head]:
    """The roster -- reflects the models actually on the box, plus one dormant
    MoE slot that wakes up when the hardware can hold it."""
    ram = _free_ram_mb() if free_ram_mb is None else free_ram_mb
    moe_enabled = ram >= 24000
    return [
        Head("qwen3.5-9b", "general assistant -- the body's default voice",
             "general", footprint_mb=6600, priority=60),
        Head("qwen3.5-9b-abliterated", "uncensored -- private roleplay / companion",
             "uncensored", footprint_mb=6600, priority=60),
        Head("qwen2.5-14b", "deep reasoning / self-edit review",
             "reason", footprint_mb=7600, priority=55),
        Head("qwen2.5-coder-14b", "heavy coding",
             "code", footprint_mb=7600, priority=55),
        Head("qwen2.5-coder-3b-cpu", "fast code drafts (CPU lane)",
             "draft", gpu=False, requires_ram_mb=2200, priority=40),
        Head("qwen2.5-1.5b-cpu", "intent classifier (CPU lane)",
             "classify", gpu=False, requires_ram_mb=1500, priority=40),
        # DORMANT -- the big MoE "deep" brain: 30B total / ~3B active, uncensored,
        # experts offloaded to host RAM. Lights up when ~24GB host RAM is free
        # (a Q4 fit without 2-bit brain-damage). See MAGIC-5.0-HYDRA-ROADMAP.md.
        Head("qwen3-30b-a3b",
             "MoE deep brain -- 30B total / 3B active, uncensored, experts in RAM",
             "deep", footprint_mb=6500, requires_ram_mb=24000, priority=80,
             enabled=moe_enabled, note="dormant -- lights up at ~24GB free RAM"),
    ]


class Hydra:
    def __init__(self, heads: Optional[list[Head]] = None):
        self.heads = heads if heads is not None else default_heads()

    def by_name(self, name: str) -> Optional[Head]:
        for h in self.heads:
            if h.name == name:
                return h
        return None

    def _vram_budget(self, free_vram_mb: float, resident: Optional[str] = None,
                     swappable_vram_mb: float = 0.0) -> float:
        budget = float(free_vram_mb or 0.0) + float(swappable_vram_mb or 0.0)
        if resident and not swappable_vram_mb:
            res = self.by_name(resident)
            if res is not None and res.gpu and res.footprint_mb:
                budget += res.footprint_mb
        return budget

    def available(self, free_vram_mb: float, free_ram_mb: float,
                  resident: Optional[str] = None,
                  swappable_vram_mb: float = 0.0) -> list[Head]:
        """Enabled heads that fit the hardware right now (counting swappable
        resident VRAM so an active 9B isn't marked unavailable by its own
        VRAM footprint)."""
        vram_budget = self._vram_budget(free_vram_mb, resident=resident,
                                        swappable_vram_mb=swappable_vram_mb)
        out = []
        for h in self.heads:
            if not h.enabled:
                continue
            if h.gpu and h.footprint_mb and h.footprint_mb > vram_budget:
                continue
            if h.requires_ram_mb and h.requires_ram_mb > free_ram_mb:
                continue
            out.append(h)
        return out

    def route(self, task: str, free_vram_mb: float, free_ram_mb: float,
              resident: Optional[str] = None,
              swappable_vram_mb: float = 0.0) -> Optional[Head]:
        """Best available head for a task. Swap-cost-aware on a one-at-a-time
        GPU: if the resident head already serves this task, stay put rather than
        pay a cold model load off the HDD."""
        avail = self.available(free_vram_mb, free_ram_mb, resident=resident,
                               swappable_vram_mb=swappable_vram_mb)
        if not avail:
            return None
        wanted_kinds = KIND_FOR_TASK.get(task, ["general"])
        pool = []
        for k in wanted_kinds:
            pool = [h for h in avail if h.kind == k]
            if pool:
                break
        if not pool:
            pool = avail
        best = max(pool, key=lambda h: (h.priority, h.name))
        # swap-cost awareness: if the resident GPU head is in the acceptable pool
        # and the best pick isn't strictly its kind, keep the resident one.
        if resident:
            res = self.by_name(resident)
            if res in pool and res.kind == best.kind:
                return res
        return best

    def select(self, message: str, free_vram_mb: float, free_ram_mb: float,
               resident: Optional[str] = None, private: bool = False,
               swappable_vram_mb: float = 0.0):
        """(task, head) -- what Magic would route this request to, on its own.
        This is the auto-router: no user command, no model name, just Magic
        picking the best head that fits. Returns the chosen Head (or None)."""
        task = classify_task(message, private=private)
        head = self.route(task, free_vram_mb, free_ram_mb, resident=resident,
                          swappable_vram_mb=swappable_vram_mb)
        return task, head

    def report(self, free_vram_mb: Optional[float] = None,
               free_ram_mb: Optional[float] = None,
               resident: Optional[str] = None,
               swappable_vram_mb: float = 0.0) -> list[dict]:
        """Status of every head -- for a /hydra endpoint and Magic's self-report."""
        vram_budget = (self._vram_budget(free_vram_mb, resident=resident,
                                         swappable_vram_mb=swappable_vram_mb)
                       if free_vram_mb is not None else None)
        rows = []
        for h in self.heads:
            fits = True
            if vram_budget is not None and h.gpu and h.footprint_mb:
                fits = fits and h.footprint_mb <= vram_budget
            if free_ram_mb is not None and h.requires_ram_mb:
                fits = fits and h.requires_ram_mb <= free_ram_mb
            state = ("disabled" if not h.enabled
                     else "ready" if (free_vram_mb is None and free_ram_mb is None)
                     else "available" if fits else "needs-hardware")
            rows.append({
                "name": h.name, "specialty": h.specialty, "kind": h.kind,
                "lane": "gpu" if h.gpu else "cpu", "footprint_mb": h.footprint_mb,
                "requires_ram_mb": h.requires_ram_mb, "state": state, "note": h.note,
            })
        return rows
