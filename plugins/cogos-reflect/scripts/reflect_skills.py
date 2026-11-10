#!/usr/bin/env python3
"""reflect_skills.py: inventory every skill with usage and staleness evidence, for curation.

  reflect_skills.py [--bundle RUN/bundle.json] [--out RUN/skills.json]

Per skill: name, path, home (claude|hermes|plugin), description length, size, uses (from the bundle's
skill_invocations + Hermes .usage.json when present), last used, broken path references (absolute paths
in the body that no longer exist), and near-duplicate descriptions. Read-only. Prints a summary.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from reflect_common import content_words, jaccard, latest_run_dir, write_json  # noqa: E402

FM = re.compile(r"^---\n(.*?)\n---\n", re.S)


def frontmatter(text: str) -> dict:
    m = FM.match(text)
    if not m:
        return {}
    out = {}
    for line in m.group(1).splitlines():
        mm = re.match(r"^(name|description):\s*(.*)$", line)
        if mm:
            out[mm.group(1)] = mm.group(2).strip().strip('"').strip("'")
    return out


def skill_dirs() -> list[tuple[str, Path]]:
    home = Path(os.environ.get("HERMES_REAL_HOME", str(Path.home())))
    out = [("claude", home / ".claude" / "skills"), ("claude-project", Path.cwd() / ".claude" / "skills")]
    out += [("hermes", p) for p in sorted((home / ".hermes" / "profiles").glob("*/skills"))]
    out += [("hermes", home / ".hermes" / "skills")]
    out += [("extra", Path(p).expanduser()) for p in os.environ.get("REFLECT_SKILL_DIRS", "").split(os.pathsep) if p]
    return [(k, d) for k, d in out if d.is_dir()]


def hermes_usage() -> dict:
    home = Path(os.environ.get("HERMES_REAL_HOME", str(Path.home())))
    merged = {}
    for p in list((home / ".hermes" / "profiles").glob("*/skills/.usage.json")) + [home / ".hermes" / "skills" / ".usage.json"]:
        try:
            for k, v in json.loads(p.read_text()).items():
                cur = merged.get(k)
                if not cur or (v.get("use_count") or 0) > (cur.get("use_count") or 0):
                    merged[k] = v
        except (OSError, json.JSONDecodeError):
            continue
    return merged


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--bundle")
    ap.add_argument("--out")
    ap.add_argument("--overlap", type=float, default=0.55, help="description Jaccard counted as overlap")
    a = ap.parse_args(argv)
    run = latest_run_dir()
    bundle_p = Path(a.bundle) if a.bundle else (run / "bundle.json" if run else None)
    invoked = {}
    if bundle_p and bundle_p.is_file():
        invoked = json.loads(bundle_p.read_text()).get("skill_invocations", {})
    usage = hermes_usage()

    skills, seen = [], set()
    for home_kind, d in skill_dirs():
        for f in d.rglob("SKILL.md"):
            s = str(f)
            if "/.archive" in s or "/_archive/" in s or "/.curator_backups/" in s:
                continue
            try:
                text = f.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            fm = frontmatter(text)
            name = fm.get("name") or f.parent.name
            if (home_kind, name) in seen:
                continue
            seen.add((home_kind, name))
            body = text[FM.match(text).end():] if FM.match(text) else text
            refs = set(re.findall(r"(?<![\w$])(/(?:home|Users|mnt|opt|etc|usr)/[\w.\-/]+)", body))
            # Only judge paths this machine could have: a ref under /Users on a Linux node (or /home/<other>)
            # belongs to another node, not a broken one.
            def local(r):
                parts = Path(r).parts
                return len(parts) > 2 and Path(*parts[:3]).exists()
            broken = sorted(r for r in refs if "<" not in r and local(r)
                            and not Path(r.rstrip(".,:;)")).exists())[:8]
            foreign = sum(1 for r in refs if not local(r))
            u = usage.get(name, {})
            skills.append({
                "name": name, "home": home_kind, "path": s, "bytes": len(text),
                "description": fm.get("description", "")[:300], "desc_len": len(fm.get("description", "")),
                "uses_in_window": invoked.get(name, 0), "uses_total": u.get("use_count"),
                "last_used": u.get("last_used_at"), "state": u.get("state"), "pinned": u.get("pinned"),
                "created_by": u.get("created_by"), "broken_refs": broken, "foreign_refs": foreign,
            })
    # overlap
    words = [set(content_words(s["name"] + " " + s["description"])) for s in skills]
    for i, s in enumerate(skills):
        ov = []
        for j, t in enumerate(skills):
            if i != j and t["name"] != s["name"] and words[i] and jaccard(words[i], words[j]) >= a.overlap:
                ov.append(t["name"])
            elif i != j and t["name"] == s["name"] and t["home"] != s["home"]:
                s.setdefault("also_installed_in", []).append(t["home"])
        s["overlaps"] = ov[:5]
    out = Path(a.out) if a.out else ((run / "skills.json") if run else Path("skills.json"))
    write_json(out, {"kind": "skills", "skills": skills})
    print(json.dumps({
        "out": str(out), "skills": len(skills),
        "unused_in_window": sum(1 for s in skills if not s["uses_in_window"] and not (s["uses_total"] or 0)),
        "with_broken_refs": sum(1 for s in skills if s["broken_refs"]),
        "with_overlap": sum(1 for s in skills if s["overlaps"]),
        "long_descriptions": sum(1 for s in skills if s["desc_len"] > 200),
    }, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
