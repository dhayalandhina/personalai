#!/usr/bin/env python3
"""
skill_loader.py — progressive-disclosure skill library for dd (or any agent loop).

Agent Skills format (one folder per skill):
    skills/<name>/SKILL.md        YAML frontmatter (name, description, ...) + instructions
    skills/<name>/scripts/*.py|sh runnable helpers (every script supports --selftest)
    skills/<name>/references/*.md optional extra docs loaded on demand

Three levels of disclosure (keeps small local models fast):
    1. index_prompt()   -> name + description only, goes into the system prompt at boot
    2. load(name)       -> full SKILL.md body when the skill is triggered
    3. reference(name,f)-> a reference file only when the SKILL.md tells you to read it

Usage inside dd's loop:
    from skill_loader import SkillLibrary
    lib = SkillLibrary()                      # defaults to ./skills next to this file
    system_prompt += lib.index_prompt()
    hits = lib.match(user_message)            # cheap keyword routing for small models
    body = lib.load("web-search")             # full instructions
    out  = lib.run("web-search", "search.py", ["ollama gguf", "--max", "5"])

Ollama / OpenAI-style tool calling:
    tools = lib.tool_schemas()                # load_skill, run_skill_script, list_skills
    result = lib.dispatch(tool_name, tool_args)
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

MAX_NAME = 64
MAX_DESC = 1024
NAME_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")


@dataclass
class Skill:
    name: str
    description: str
    path: Path
    meta: dict = field(default_factory=dict)
    triggers: list[str] = field(default_factory=list)

    @property
    def skill_md(self) -> Path:
        return self.path / "SKILL.md"

    @property
    def scripts(self) -> list[str]:
        d = self.path / "scripts"
        return sorted(p.name for p in d.iterdir() if p.is_file()) if d.is_dir() else []

    @property
    def references(self) -> list[str]:
        d = self.path / "references"
        return sorted(p.name for p in d.iterdir() if p.is_file()) if d.is_dir() else []


def parse_frontmatter(text: str) -> tuple[dict, str]:
    """Minimal YAML frontmatter parser (key: value, lists as [a, b] or '- item')."""
    if not text.startswith("---"):
        return {}, text
    end = text.find("\n---", 3)
    if end == -1:
        return {}, text
    raw = text[3:end].strip("\n")
    body = text[end + 4:].lstrip("\n")
    meta: dict[str, Any] = {}
    key = None
    for line in raw.splitlines():
        if not line.strip():
            continue
        if line.startswith((" ", "\t")) and key:
            item = line.strip()
            if item.startswith("- "):
                meta.setdefault(key, [])
                if isinstance(meta[key], list):
                    meta[key].append(item[2:].strip().strip('"\''))
            elif ":" in item and isinstance(meta.get(key), dict):
                k, v = item.split(":", 1)
                meta[key][k.strip()] = v.strip().strip('"\'')
            continue
        if ":" not in line:
            continue
        key, val = line.split(":", 1)
        key, val = key.strip(), val.strip()
        if val == "":
            meta[key] = {}  # nested block follows (metadata:) — may become list
        elif val.startswith("[") and val.endswith("]"):
            meta[key] = [v.strip().strip('"\'') for v in val[1:-1].split(",") if v.strip()]
        else:
            meta[key] = val.strip('"\'')
    # empty dict placeholders that got list items were set above; normalize
    for k, v in list(meta.items()):
        if v == {}:
            meta[k] = ""
    return meta, body


def validate_skill(path: Path) -> list[str]:
    """Return a list of problems (empty list == valid)."""
    problems: list[str] = []
    md = path / "SKILL.md"
    if not md.is_file():
        return [f"{path.name}: missing SKILL.md"]
    meta, body = parse_frontmatter(md.read_text(encoding="utf-8"))
    name = meta.get("name", "")
    desc = meta.get("description", "")
    if not name:
        problems.append(f"{path.name}: frontmatter missing 'name'")
    elif name != path.name:
        problems.append(f"{path.name}: name '{name}' must equal folder name")
    elif not NAME_RE.match(name) or len(name) > MAX_NAME:
        problems.append(f"{path.name}: name must be lowercase-hyphen, <= {MAX_NAME} chars")
    if not desc:
        problems.append(f"{path.name}: frontmatter missing 'description'")
    elif len(desc) > MAX_DESC:
        problems.append(f"{path.name}: description > {MAX_DESC} chars")
    if len(body.strip()) < 40:
        problems.append(f"{path.name}: SKILL.md body is empty")
    for sub in ("scripts", "references", "assets"):
        d = path / sub
        if d.exists() and not d.is_dir():
            problems.append(f"{path.name}: {sub} must be a directory")
    return problems


class SkillLibrary:
    def __init__(self, root: str | os.PathLike | None = None):
        self.root = Path(root) if root else Path(__file__).resolve().parent / "skills"
        self.skills: dict[str, Skill] = {}
        self.reload()

    # ---------- discovery ----------
    def reload(self) -> None:
        self.skills.clear()
        if not self.root.is_dir():
            return
        for d in sorted(self.root.iterdir()):
            if not d.is_dir() or not (d / "SKILL.md").is_file():
                continue
            meta, _ = parse_frontmatter((d / "SKILL.md").read_text(encoding="utf-8"))
            name = meta.get("name") or d.name
            trig = meta.get("triggers") or []
            if isinstance(trig, str):
                trig = [t.strip() for t in trig.split(",") if t.strip()]
            self.skills[name] = Skill(name=name, description=meta.get("description", ""),
                                      path=d, meta=meta, triggers=[t.lower() for t in trig])

    def names(self) -> list[str]:
        return list(self.skills)

    # ---------- level 1: index ----------
    def index_prompt(self) -> str:
        lines = ["<available_skills>",
                 "Load a skill with load_skill(name) BEFORE doing the task it covers.",
                 "Run its helpers with run_skill_script(name, script, args)."]
        for s in self.skills.values():
            lines.append(f"- {s.name}: {s.description}")
        lines.append("</available_skills>")
        return "\n".join(lines)

    # ---------- level 2: full skill ----------
    def load(self, name: str) -> str:
        s = self._get(name)
        _, body = parse_frontmatter(s.skill_md.read_text(encoding="utf-8"))
        extras = []
        if s.scripts:
            extras.append("scripts: " + ", ".join(s.scripts))
        if s.references:
            extras.append("references: " + ", ".join(s.references))
        tail = ("\n\n[" + " | ".join(extras) + "]") if extras else ""
        return f"<skill name=\"{s.name}\" path=\"{s.path}\">\n{body.rstrip()}{tail}\n</skill>"

    # ---------- level 3: reference ----------
    def reference(self, name: str, filename: str) -> str:
        s = self._get(name)
        p = (s.path / "references" / filename).resolve()
        if not str(p).startswith(str(s.path.resolve())) or not p.is_file():
            raise FileNotFoundError(f"{name}: no reference '{filename}'")
        return p.read_text(encoding="utf-8")

    # ---------- run a helper ----------
    def run(self, name: str, script: str, args: list[str] | None = None,
            timeout: int = 600, env: dict | None = None) -> dict:
        s = self._get(name)
        p = (s.path / "scripts" / script).resolve()
        if not str(p).startswith(str(s.path.resolve())) or not p.is_file():
            raise FileNotFoundError(f"{name}: no script '{script}' (have: {s.scripts})")
        cmd = [sys.executable, str(p)] if p.suffix == ".py" else ["bash", str(p)]
        cmd += [str(a) for a in (args or [])]
        full_env = {**os.environ, "DD_SKILL_DIR": str(s.path), **(env or {})}
        try:
            r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, env=full_env)
            return {"ok": r.returncode == 0, "code": r.returncode,
                    "stdout": r.stdout[-20000:], "stderr": r.stderr[-4000:]}
        except subprocess.TimeoutExpired:
            return {"ok": False, "code": -1, "stdout": "", "stderr": f"timeout after {timeout}s"}

    # ---------- cheap routing for small local models ----------
    def match(self, text: str, top: int = 3) -> list[tuple[str, int]]:
        t = text.lower()
        words = set(re.findall(r"[a-z0-9]+", t))
        scored = []
        for s in self.skills.values():
            score = 0
            for trig in s.triggers:
                if trig in t:
                    score += 3
            for w in re.findall(r"[a-z0-9]+", s.name):
                if w in words:
                    score += 2
            for w in re.findall(r"[a-z0-9]{4,}", s.description.lower()):
                if w in words:
                    score += 1
            if score:
                scored.append((s.name, score))
        scored.sort(key=lambda x: -x[1])
        return scored[:top]

    # ---------- tool calling glue ----------
    def tool_schemas(self) -> list[dict]:
        return [
            {"type": "function", "function": {
                "name": "list_skills",
                "description": "List available skills with one-line descriptions.",
                "parameters": {"type": "object", "properties": {}}}},
            {"type": "function", "function": {
                "name": "load_skill",
                "description": "Load the full instructions of a skill before doing its task.",
                "parameters": {"type": "object", "required": ["name"],
                               "properties": {"name": {"type": "string"}}}}},
            {"type": "function", "function": {
                "name": "run_skill_script",
                "description": "Run a helper script that belongs to a skill.",
                "parameters": {"type": "object", "required": ["name", "script"],
                               "properties": {"name": {"type": "string"},
                                              "script": {"type": "string"},
                                              "args": {"type": "array", "items": {"type": "string"}}}}}},
            {"type": "function", "function": {
                "name": "read_skill_reference",
                "description": "Read a reference file of a skill.",
                "parameters": {"type": "object", "required": ["name", "file"],
                               "properties": {"name": {"type": "string"}, "file": {"type": "string"}}}}},
        ]

    def dispatch(self, tool: str, args: dict | None = None) -> str:
        args = args or {}
        try:
            if tool == "list_skills":
                return self.index_prompt()
            if tool == "load_skill":
                return self.load(args["name"])
            if tool == "run_skill_script":
                return json.dumps(self.run(args["name"], args["script"], args.get("args") or []))
            if tool == "read_skill_reference":
                return self.reference(args["name"], args["file"])
            return f"unknown tool {tool}"
        except Exception as e:  # tool errors go back to the model, never crash the loop
            return f"error: {e}"

    # ---------- internals ----------
    def _get(self, name: str) -> Skill:
        if name not in self.skills:
            raise KeyError(f"unknown skill '{name}' (have: {', '.join(self.skills) or 'none'})")
        return self.skills[name]


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser(description="dd skill library CLI")
    ap.add_argument("--root", default=None)
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("index")
    sub.add_parser("validate")
    p = sub.add_parser("load"); p.add_argument("name")
    p = sub.add_parser("match"); p.add_argument("text")
    p = sub.add_parser("run"); p.add_argument("name"); p.add_argument("script"); p.add_argument("args", nargs="*")
    a = ap.parse_args()
    lib = SkillLibrary(a.root)
    if a.cmd == "index":
        print(lib.index_prompt())
    elif a.cmd == "validate":
        bad = [m for s in lib.skills.values() for m in validate_skill(s.path)]
        print("\n".join(bad) if bad else f"OK: {len(lib.skills)} skills valid")
        sys.exit(1 if bad else 0)
    elif a.cmd == "load":
        print(lib.load(a.name))
    elif a.cmd == "match":
        print(json.dumps(lib.match(a.text)))
    elif a.cmd == "run":
        print(json.dumps(lib.run(a.name, a.script, a.args), indent=1))
