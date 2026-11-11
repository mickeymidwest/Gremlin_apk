"""Recipes -- user-authored, parameterized, repeatable workflows (Goose's
'recipes'). A recipe is a YAML file under data/recipes/<name>.yaml:

    name: audit-service
    description: Security sweep of a systemd service
    mode: plan            # chat | plan | build  (see MODES)
    parameters:
      - name: service
        required: true
      - name: depth
        default: quick
    prompt: |
      Audit the {service} service ({depth} pass): check its systemd unit,
      open ports, and file permissions. Report findings.

Running a recipe fills {param} placeholders from the given args and feeds the
result to the entrypoint its MODE selects. The modes map onto what Magic
already has, made explicit (like OpenCode's build/plan/explore agents):

  * chat  -> an ordinary mediated answer (reply.answer)
  * plan  -> a READ-ONLY research subagent / `/do` (look, never touch)
  * build -> a full battle (`/fix`-style: edit + verify + deliver-when-green)

This module is the deterministic core: discover, validate, render. It never
runs anything itself (no model, no side effects), so it is safe to call
anywhere; the caller dispatches the rendered prompt by mode.
"""
from __future__ import annotations

import re
from pathlib import Path

MODES = ("chat", "plan", "build")
_DIR = "data/recipes"
_PLACEHOLDER = re.compile(r"\{(\w+)\}")


def recipes_dir(root: str) -> Path:
    return Path(root) / _DIR


def _parse(path: Path) -> "dict | None":
    try:
        import yaml
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except Exception:
        return None
    if not isinstance(data, dict):
        return None
    data.setdefault("name", path.stem)
    return data


def validate(recipe: dict) -> list:
    """Problems with a recipe, or [] if it is well-formed."""
    errs = []
    if not isinstance(recipe, dict):
        return ["not a mapping"]
    if not str(recipe.get("name", "")).strip():
        errs.append("missing name")
    if not str(recipe.get("prompt", "")).strip():
        errs.append("missing prompt")
    mode = recipe.get("mode", "chat")
    if mode not in MODES:
        errs.append(f"mode must be one of {MODES} (got {mode!r})")
    params = recipe.get("parameters", []) or []
    if not isinstance(params, list):
        errs.append("parameters must be a list")
    else:
        for p in params:
            if not isinstance(p, dict) or not p.get("name"):
                errs.append(f"bad parameter entry: {p!r}")
    # every {placeholder} in the prompt should be a declared parameter
    declared = {p.get("name") for p in params if isinstance(p, dict)}
    for ph in set(_PLACEHOLDER.findall(str(recipe.get("prompt", "")))):
        if ph not in declared:
            errs.append(f"prompt uses {{{ph}}} but no parameter named {ph!r} is declared")
    return errs


def list_recipes(root: str) -> list:
    """[{name, description, mode, params:[names], ok:bool, errors:[...]}], sorted."""
    d = recipes_dir(root)
    out = []
    if not d.is_dir():
        return out
    for path in sorted(d.glob("*.yaml")):
        r = _parse(path)
        if r is None:
            out.append({"name": path.stem, "description": "", "mode": "?",
                        "params": [], "ok": False, "errors": ["could not parse YAML"]})
            continue
        errs = validate(r)
        out.append({
            "name": r.get("name", path.stem),
            "description": r.get("description", ""),
            "mode": r.get("mode", "chat"),
            "params": [p.get("name") for p in (r.get("parameters") or []) if isinstance(p, dict)],
            "ok": not errs, "errors": errs,
        })
    return out


def load_recipe(root: str, name: str) -> "dict | None":
    p = recipes_dir(root) / f"{name}.yaml"
    return _parse(p) if p.exists() else None


def render(recipe: dict, args: dict) -> dict:
    """Fill the recipe's prompt from args + parameter defaults.
    Returns {ok, prompt, mode, missing:[required names not supplied], errors}."""
    errs = validate(recipe)
    if errs:
        return {"ok": False, "prompt": "", "mode": recipe.get("mode", "chat"),
                "missing": [], "errors": errs}
    args = args or {}
    values, missing = {}, []
    for p in recipe.get("parameters", []) or []:
        name = p.get("name")
        if name in args and str(args[name]) != "":
            values[name] = str(args[name])
        elif "default" in p:
            values[name] = str(p["default"])
        elif p.get("required"):
            missing.append(name)
        else:
            values[name] = ""
    if missing:
        return {"ok": False, "prompt": "", "mode": recipe.get("mode", "chat"),
                "missing": missing, "errors": []}

    def _sub(m):
        return values.get(m.group(1), m.group(0))
    prompt = _PLACEHOLDER.sub(_sub, str(recipe.get("prompt", "")))
    return {"ok": True, "prompt": prompt.strip(), "mode": recipe.get("mode", "chat"),
            "missing": [], "errors": []}


def parse_args(tokens: list) -> dict:
    """`key=value` tokens -> {key: value}; a bare token with no '=' is ignored
    (the caller already consumed the recipe name)."""
    out = {}
    for tok in tokens or []:
        if "=" in tok:
            k, v = tok.split("=", 1)
            out[k.strip()] = v.strip()
    return out
