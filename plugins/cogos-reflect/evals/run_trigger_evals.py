#!/usr/bin/env python3
"""Trigger evals: does an agent reach for the right reflect skill from the description alone?

Builds a skill index (name + description) from this plugin's skills plus a set of decoy skills
an agent would also see, then asks a model, per prompt, which ONE skill it would load first.
Model call goes through `claude -p` (operator's own subscription; no API key handling here).

  run_trigger_evals.py [--model sonnet] [--decoys N]
Prints per-case results and a pass rate. Exit 0 if pass rate >= --min (default 0.85).
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
SKILLS = HERE.parent / "skills"

DECOYS = {
    "code-review": "Use when reviewing a pull request or diff for bugs.",
    "session-search": "Use when recalling what was said or decided in a past conversation.",
    "skill-creator": "Use when the user asks to create or edit a skill.",
    "github": "GitHub workflow: issues, PRs, reviews, CI.",
    "summarize": "Use when asked to summarize an article, page, or document.",
    "hermes-agent": "Configure, extend, or troubleshoot Hermes Agent (gateway, config, tools).",
    "friction-ledger": "Use at the end of a substantial session to record what resisted.",
    "memory-curation": "Use when Tier-1 memory nears its cap. Offload, never delete.",
    "debugging": "Systematic debugging with debuggers and logs.",
    "plan": "Write a markdown plan; no execution.",
}


def index() -> dict[str, str]:
    out = {}
    for d in sorted(SKILLS.iterdir()):
        t = (d / "SKILL.md").read_text()
        m = re.search(r'^description:\s*"?(.*?)"?\s*$', t, re.M)
        out[d.name] = m.group(1) if m else ""
    return out


def ask(model: str, idx: dict[str, str], prompt: str) -> str:
    listing = "\n".join(f"- {k}: {v}" for k, v in idx.items())
    q = (f"You are an agent. Available skills (name: description):\n{listing}\n\n"
         f"The operator says: \"{prompt}\"\n\nWhich ONE skill would you load first? If none fits, answer none. "
         f"Reply with only the skill name.")
    p = subprocess.run(["claude", "-p", "--model", model, q], capture_output=True, text=True, timeout=120)
    ans = (p.stdout or "").strip().splitlines()[-1].strip().strip("`*.").lower() if p.stdout.strip() else "error"
    return ans


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", default="sonnet")
    ap.add_argument("--min", type=float, default=0.85)
    a = ap.parse_args()
    idx = {**index(), **DECOYS}
    cases = json.loads((HERE / "triggers.json").read_text())["cases"]
    suite = set(index())
    ok = 0
    rows = []
    for c in cases:
        got = ask(a.model, idx, c["prompt"])
        exp = c["expect"]
        hit = (got in exp) or ("none" in exp and got not in suite)
        ok += hit
        rows.append({"prompt": c["prompt"], "expect": exp, "got": got, "pass": hit})
        print(("PASS " if hit else "FAIL ") + f"{got:<18} <- {c['prompt']}  (expect {'/'.join(exp)})", flush=True)
    rate = ok / len(cases)
    print(json.dumps({"model": a.model, "cases": len(cases), "passed": ok, "rate": round(rate, 3)}))
    (HERE / "last_trigger_run.json").write_text(json.dumps({"model": a.model, "rate": rate, "rows": rows}, indent=1))
    return 0 if rate >= a.min else 1


if __name__ == "__main__":
    sys.exit(main())
