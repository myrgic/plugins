#!/usr/bin/env python3
"""reflect_render.py: turn run artifacts into what the operator sees.

  reflect_render.py findings [--run R] [--html]   findings.json -> summary (stdout) [+ report.html]
  reflect_render.py cards    [--run R]            proposals/*/PROPOSAL.md -> cards.md (one card each)
  reflect_render.py check    [--run R]            validate findings.json / proposals against the schemas

The agent writes findings.json and proposals; this script only formats and validates them, so every
run looks the same and nothing is pasted by hand.
"""
from __future__ import annotations

import argparse
import html
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from reflect_common import latest_run_dir, runs_home  # noqa: E402

FINDING_REQ = ["id", "kind", "title", "sessions", "occurrences", "evidence", "impact", "suggested_mechanism"]
PROPOSAL_FIELDS = ["What changes", "What you get", "Why you might say no", "Mechanism", "Evidence", "Exact change"]


def run_dir(arg: str | None) -> Path:
    if arg:
        p = (runs_home() / arg).resolve()
        if runs_home().resolve() not in p.parents or not p.is_dir():
            print(f"error: no run {arg!r} under {runs_home()}", file=sys.stderr)
            sys.exit(1)
        return p
    r = latest_run_dir()
    if not r:
        print("error: no runs yet; run gather-history first", file=sys.stderr)
        sys.exit(1)
    return r


def check(rd: Path) -> list[str]:
    errs = []
    fp = rd / "findings.json"
    if fp.is_file():
        try:
            fj = json.loads(fp.read_text())
            for i, f in enumerate(fj.get("findings", [])):
                miss = [k for k in FINDING_REQ if k not in f]
                if miss:
                    errs.append(f"findings[{i}] missing {miss}")
                if not f.get("evidence"):
                    errs.append(f"findings[{i}] {f.get('id')} has no evidence ids")
        except json.JSONDecodeError as e:
            errs.append(f"findings.json invalid JSON: {e}")
    for p in sorted((rd / "proposals").glob("*/PROPOSAL.md")):
        t = p.read_text()
        miss = [k for k in PROPOSAL_FIELDS if f"**{k}" not in t]
        if miss:
            errs.append(f"{p.parent.name}: PROPOSAL.md missing {miss}")
        if not (p.parent / "EVIDENCE.md").is_file():
            errs.append(f"{p.parent.name}: no EVIDENCE.md")
    return errs


def summary(fj: dict) -> str:
    fs = fj.get("findings", [])
    lines = [fj.get("headline", "").strip()] if fj.get("headline") else []
    for f in sorted(fs, key=lambda x: (-x.get("sessions", 0), -x.get("occurrences", 0)))[:5]:
        lines.append(f"- {f['title']} ({f.get('sessions', '?')} sessions, {f.get('occurrences', '?')}x) "
                     f"-> {f.get('suggested_mechanism', '?')}")
    return "\n".join(lines)


def html_report(fj: dict, bundle: dict | None) -> str:
    e = html.escape
    t = (bundle or {}).get("totals", {})
    rows = "".join(
        f"<tr><td>{e(f['id'])}</td><td>{e(f['kind'])}</td><td>{e(f['title'])}</td><td>{f.get('sessions', '')}</td>"
        f"<td>{f.get('occurrences', '')}</td><td>{e(str(f.get('impact', '')))}</td>"
        f"<td>{e(str(f.get('suggested_mechanism', '')))}</td></tr>" for f in fj.get("findings", []))
    secs = ""
    for key, label in (("working", "What's working"), ("claude_side", "Agent-side friction"),
                       ("operator_side", "Operator-side friction")):
        if fj.get(key):
            items = "".join(f"<li>{e(x)}</li>" for x in fj[key])
            secs += f"<h2>{label}</h2><ul>{items}</ul>"
    return f"""<!doctype html><meta charset="utf-8"><title>reflect: {e(fj.get('run', ''))}</title>
<style>body{{font:14px/1.5 system-ui,sans-serif;max-width:1100px;margin:2em auto;padding:0 1em;color:#1d1d1f}}
table{{border-collapse:collapse;width:100%}}td,th{{border-bottom:1px solid #ddd;padding:6px;text-align:left;vertical-align:top}}
th{{background:#f5f5f7}}.k{{display:inline-block;margin-right:2em}}.k b{{font-size:20px;display:block}}</style>
<h1>How we work: {e(fj.get('window', ''))}</h1><p>{e(fj.get('headline', ''))}</p>
<div><span class=k><b>{t.get('operator_sessions', '?')}</b>operator sessions</span>
<span class=k><b>{t.get('automated_sessions', '?')}</b>automated</span>
<span class=k><b>{t.get('tool_calls', '?')}</b>tool calls</span>
<span class=k><b>{t.get('correction_rate', '?')}</b>correction rate</span></div>
{secs}<h2>Findings</h2><table><tr><th>id</th><th>kind</th><th>finding</th><th>sessions</th><th>x</th>
<th>impact</th><th>cheapest fix</th></tr>{rows}</table>
<p style="color:#888">Counts are computed from local history; themes and impact are model judgments.</p>"""


def cards(rd: Path) -> str:
    out = []
    ps = sorted((rd / "proposals").glob("*/PROPOSAL.md"))
    for i, p in enumerate(ps, 1):
        t = p.read_text().strip()
        t = re.sub(r"^# .*", f"### Proposal {i} of {len(ps)}: " + p.parent.name, t, count=1) if t.startswith("# ") \
            else f"### Proposal {i} of {len(ps)}: {p.parent.name}\n{t}"
        out.append(t + f"\n\n`{p.parent}`")
    return "\n\n---\n\n".join(out) if out else "No proposals in this run."


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("what", choices=["findings", "cards", "check"])
    ap.add_argument("--run")
    ap.add_argument("--html", action="store_true")
    a = ap.parse_args(argv)
    rd = run_dir(a.run)
    if a.what == "check":
        errs = check(rd)
        print(json.dumps({"run": rd.name, "ok": not errs, "errors": errs}, indent=1))
        return 0 if not errs else 1
    if a.what == "findings":
        fp = rd / "findings.json"
        if not fp.is_file():
            print("error: no findings.json in run; run find-friction first", file=sys.stderr)
            return 1
        fj = json.loads(fp.read_text())
        print(summary(fj))
        if a.html:
            bp = rd / "bundle.json"
            (rd / "report.html").write_text(html_report(fj, json.loads(bp.read_text()) if bp.is_file() else None))
            print(f"\nreport: {rd / 'report.html'}")
        return 0
    if a.what == "cards":
        c = cards(rd)
        (rd / "cards.md").write_text(c)
        print(c)
        return 0
    return 2


if __name__ == "__main__":
    sys.exit(main())
