#!/usr/bin/env python3
"""reflect_backfill.py: seed the ledger from lessons already written down (CogOS reflective store).

  reflect_backfill.py [--dry] [--root DIR ...]

Finds lesson-shaped bullets in retros, friction ledgers and consolidations: lines under headings like
"Lessons", "My own method failures", "What I'd do differently", "Root cause", "Rules". Each becomes an
`observed` ledger row (id prefix L-, kind "lesson", source "backfill:<cogdoc path>"), so later runs can
ask "did this written lesson stop the friction?". Idempotent: ids are content hashes.
Prints a summary (and every lesson with --dry).
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from reflect_common import find_cog_workspace, quote, stable_id  # noqa: E402

HEAD = re.compile(r"(?im)^#{1,4}\s*(lessons?( learned)?|my own method failures|method failures|what i'?d do differently|"
                  r"what went wrong|root causes?|rules?( learned)?|takeaways|anti-?patterns|pitfalls|what to do next time|"
                  r"substrate\s*/?\s*tooling friction|tooling friction|.*friction)\b.*$")
BULLET = re.compile(r"^\s*(?:[-*]|\d+[.)])\s+(.{25,400})$")
ITEM_HEAD = re.compile(r"^#{3,4}\s+(?:\d+[.)]\s*)?(.{15,300})$")  # friction-ledger shape: ### 7. <lesson title>


def lessons_in(path: Path) -> list[str]:
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    out, on, level = [], False, 0
    for line in text.splitlines():
        h = re.match(r"^(#{1,4})\s", line)
        if h:
            lv = len(h.group(1))
            if on and lv > level:
                m = ITEM_HEAD.match(line)
                if m:
                    out.append(re.sub(r"\*\*|`", "", m.group(1)).strip())
                continue
            on, level = bool(HEAD.match(line)), lv
            continue
        if on:
            m = BULLET.match(line)
            if m:
                out.append(re.sub(r"\*\*|`", "", m.group(1)).strip())
    return out


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--dry", action="store_true")
    ap.add_argument("--root", action="append", default=[])
    a = ap.parse_args(argv)
    roots = [Path(r) for r in a.root]
    ws = find_cog_workspace()
    if ws:
        roots += [ws / ".cog" / "mem" / "reflective", ws / ".cog" / "mem" / "episodic" / "audits"]
    roots = [r for r in roots if r.is_dir()]
    if not roots:
        print(json.dumps({"ok": False, "reason": "no lesson store found (not a CogOS workspace; pass --root)"}))
        return 3
    ledger = Path(__file__).resolve().parent / "reflect_ledger.py"
    found, files = [], 0
    for r in roots:
        for f in sorted(r.rglob("*.md")):
            if "/journal/" in str(f):
                continue  # authored reflection, not lessons to track
            ls = lessons_in(f)
            if ls:
                files += 1
            for l in ls:
                rel = str(f.relative_to(ws)) if ws and str(f).startswith(str(ws)) else str(f)
                found.append({"id": stable_id(l, prefix="L"), "title": quote(l, 200), "source": rel})
    if a.dry:
        print(json.dumps({"files": files, "lessons": len(found), "sample": found[:40]}, indent=1, ensure_ascii=False))
        return 0
    existing = json.loads(subprocess.run([sys.executable, str(ledger), "show"], capture_output=True, text=True).stdout or "[]")
    have = {f["id"] for f in existing}
    added = 0
    for x in found:
        if x["id"] in have:
            continue
        subprocess.run([sys.executable, str(ledger), "add", "--id", x["id"], "--kind", "lesson", "--title", x["title"],
                        "--source", "backfill:" + x["source"]], capture_output=True, check=True)
        added += 1
    print(json.dumps({"files": files, "lessons": len(found), "added": added, "already": len(found) - added}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
