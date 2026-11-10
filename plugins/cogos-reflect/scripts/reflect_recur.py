#!/usr/bin/env python3
"""reflect_recur.py: does this lesson already exist? Search skills, memory, rules and lesson stores.

  reflect_recur.py "words describing the lesson" [--limit 8]

Searches (whatever exists):
  skills       ~/.claude/skills, <project>/.claude/skills, Hermes profile skills, REFLECT_SKILL_DIRS
  instructions ~/.claude/CLAUDE.md, ./CLAUDE.md, ./AGENTS.md, ~/.claude/rules/*.md, Hermes memories/*.md
  memory       ~/.claude/projects/*/memory/*.md
  lessons      <cog workspace>/.cog/mem/reflective/**/*.md, .cog/mem/episodic/audits/*friction*  (CogOS)
Prints JSON hits ranked by how many distinct query words co-occur in one passage of a file
(>= 2/3 of the query words within a 3-line window).
A hit means: the lesson exists. If the friction recurred anyway, propose MOVING it to a mechanism, not
writing it again. Exit 0 always (no hits is a valid answer).
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from reflect_common import content_words, find_cog_workspace, quote  # noqa: E402


def stores() -> list[tuple[str, list[Path]]]:
    home = Path(os.environ.get("HERMES_REAL_HOME", str(Path.home())))
    cwd = Path.cwd()
    skills = [home / ".claude" / "skills", cwd / ".claude" / "skills", home / ".hermes" / "skills"]
    skills += list((home / ".hermes" / "profiles").glob("*/skills"))
    skills += [Path(p).expanduser() for p in os.environ.get("REFLECT_SKILL_DIRS", "").split(os.pathsep) if p]
    instr = [home / ".claude" / "CLAUDE.md", cwd / "CLAUDE.md", cwd / "AGENTS.md"]
    instr += list((home / ".claude" / "rules").glob("*.md"))
    instr += list((home / ".hermes").glob("memories/*.md")) + list((home / ".hermes" / "profiles").glob("*/memories/*.md"))
    memory = list((home / ".claude" / "projects").glob("*/memory/*.md"))
    lessons: list[Path] = []
    ws = find_cog_workspace()
    if ws:
        lessons += list((ws / ".cog" / "mem" / "reflective").rglob("*.md"))
        lessons += list((ws / ".cog" / "mem" / "episodic" / "audits").glob("*friction*"))
        instr += [ws / "CLAUDE.md", ws / "SOUL.md", ws / "USER.md"]
    skill_files = []
    for d in skills:
        if d.is_dir():
            skill_files += [p for p in d.rglob("SKILL.md") if "/.archive" not in str(p) and "_archive" not in str(p)
                            and "/.curator_backups/" not in str(p)]
    return [("skill", skill_files), ("instruction", instr), ("memory", memory), ("lesson", lessons)]


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("query", nargs="+")
    ap.add_argument("--limit", type=int, default=8)
    ap.add_argument("--min-words", type=int, default=2)
    a = ap.parse_args(argv)
    words = list(dict.fromkeys(content_words(" ".join(a.query))))[:8]
    if not words:
        print(json.dumps({"query": a.query, "hits": [], "note": "no content words"}))
        return 0
    pats = {w: re.compile(r"\b" + re.escape(w) + r"\w*", re.I) for w in words}
    need = min(max(a.min_words, (2 * len(words) + 2) // 3), len(words))
    hits, seen = [], set()
    for kind, files in stores():
        for f in files:
            try:
                rp = f.resolve()
                if rp in seen or not f.is_file() or f.stat().st_size > 2_000_000:
                    continue
                seen.add(rp)
                text = f.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            # Proximity, not presence: the words must co-occur within one passage (a paragraph, or a
            # 3-line window), otherwise every long skill "contains" every common lesson.
            lines = text.splitlines()
            best, best_line = [], ""
            for i in range(len(lines)):
                win = " ".join(lines[i:i + 3])
                if len(win) < 8:
                    continue
                f_ = [w for w, p in pats.items() if p.search(win)]
                if len(f_) > len(best):
                    best, best_line = f_, quote(lines[i], 200)
                    if len(best) == len(words):
                        break
            if len(best) < need:
                continue
            hits.append({"kind": kind, "path": str(f), "matched": best, "score": len(best), "line": best_line})
    order = {"skill": 0, "instruction": 1, "lesson": 2, "memory": 3}
    hits.sort(key=lambda h: (-h["score"], order[h["kind"]]))
    print(json.dumps({"query_words": words, "required_matches": need, "hits": hits[: a.limit],
                      "total_hits": len(hits)}, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
