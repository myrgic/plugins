#!/usr/bin/env python3
"""reflect_ledger.py: the append-only finding ledger shared by every cogos-reflect skill.

One JSONL file (<reflect home>/ledger.jsonl). Rows are events; a finding's state is the fold
of its rows. Nothing is ever rewritten. This script is the only writer.

  add      --id F-x --kind K --title T [--count N --sessions N --evidence id,id --source S]
  status   --id F-x --to STATUS [--reason R --ref path-or-uri]
  show     [--id F-x] [--status S] [--kind K]        folded current state, JSON
  check    --title T [--kind K]                      prior denials/duplicates for a new finding
  stats                                              counts by status and kind

Statuses: observed proposed approved denied deferred applied verified retired
Exit 0 ok, 1 not found, 2 usage error.
"""
from __future__ import annotations

import argparse
import collections
import json
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from reflect_common import SCHEMA_VERSION, content_words, epoch_to_iso, jaccard, quote, redact_path, reflect_home  # noqa: E402

STATUSES = ["observed", "proposed", "approved", "denied", "deferred", "applied", "verified", "retired"]
KINDS = ["repeated-instruction", "repeated-correction", "manual-workflow", "incantation", "tool-friction",
         "time-sink", "permission-friction", "lesson", "lesson-not-holding", "stale-skill", "overlap", "session-retro", "other"]


def ledger_path() -> Path:
    p = Path(os.environ.get("REFLECT_LEDGER") or (reflect_home() / "ledger.jsonl"))
    p.parent.mkdir(parents=True, exist_ok=True)
    return p


TEXT_FIELDS = {"title": 200, "reason": 300, "ref": 300, "source": 300}


def append(row: dict) -> dict:
    """Every persisted string passes redaction: the ledger is committed on CogOS nodes."""
    for k, cap in TEXT_FIELDS.items():
        if row.get(k):
            row[k] = quote(row[k], cap)
    if row.get("evidence"):
        row["evidence"] = [redact_path(e)[:120] for e in row["evidence"]]
    row = {"schema": SCHEMA_VERSION, "at": epoch_to_iso(time.time()), **row}
    with ledger_path().open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(row, ensure_ascii=False) + "\n")
    return row


def rows() -> list[dict]:
    p = ledger_path()
    if not p.exists():
        return []
    out = []
    for line in p.read_text(encoding="utf-8").splitlines():
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return out


def _int(v) -> int:
    try:
        return int(v or 0)
    except (TypeError, ValueError):
        return 0


def fold() -> dict[str, dict]:
    st: dict[str, dict] = {}
    for r in rows():
        fid = r.get("id")
        if not fid or r.get("op") not in ("add", "status") or (r.get("op") == "status" and r.get("to") not in STATUSES):
            continue  # malformed or hand-edited row: skip it, never crash every reader
        f = st.setdefault(fid, {"id": fid, "status": "observed", "count": 0, "sessions": 0, "evidence": [],
                                "history": [], "first_seen": r.get("at")})
        if r["op"] == "add":
            f["kind"] = r.get("kind", f.get("kind"))
            f["title"] = r.get("title", f.get("title"))
            f["count"] = max(f["count"], _int(r.get("count")))
            f["sessions"] = max(f["sessions"], _int(r.get("sessions")))
            ev = [e for e in (r.get("evidence") or []) if e not in f["evidence"]]
            f["evidence"] = (f["evidence"] + ev)[-30:]
            f["last_seen"] = r.get("at")
            f["sightings"] = f.get("sightings", 0) + 1
            if r.get("source"):
                f["source"] = r["source"]
        elif r["op"] == "status":
            f["status"] = r["to"]
            f["history"].append({"at": r.get("at"), "to": r["to"], "reason": r.get("reason"), "ref": r.get("ref")})
            if r.get("ref"):
                f["ref"] = r["ref"]
            if r["to"] == "denied":
                f["denied_reason"] = r.get("reason")
    return st


def similar(title: str, kind: str | None, threshold: float = 0.5) -> list[dict]:
    w = set(content_words(title))
    out = []
    for f in fold().values():
        if kind and f.get("kind") and f["kind"] != kind:
            continue
        sim = jaccard(w, set(content_words(f.get("title") or "")))
        if sim >= threshold:
            out.append({**{k: f.get(k) for k in ("id", "title", "kind", "status", "count", "denied_reason")},
                        "similarity": round(sim, 2)})
    return sorted(out, key=lambda x: -x["similarity"])


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    a = sub.add_parser("add")
    a.add_argument("--id", required=True)
    a.add_argument("--kind", required=True, choices=KINDS)
    a.add_argument("--title", required=True)
    a.add_argument("--count", type=int, default=0)
    a.add_argument("--sessions", type=int, default=0)
    a.add_argument("--evidence", default="")
    a.add_argument("--source", default="", help="run id, cogdoc uri, or 'session-retro'")
    s = sub.add_parser("status")
    s.add_argument("--id", required=True)
    s.add_argument("--to", required=True, choices=STATUSES)
    s.add_argument("--reason", default="")
    s.add_argument("--ref", default="")
    sh = sub.add_parser("show")
    sh.add_argument("--id")
    sh.add_argument("--status")
    sh.add_argument("--kind")
    c = sub.add_parser("check")
    c.add_argument("--title", required=True)
    c.add_argument("--kind")
    c.add_argument("--threshold", type=float, default=0.5)
    sub.add_parser("stats")
    sub.add_parser("path")
    a_ = ap.parse_args(argv)

    if a_.cmd == "path":
        print(ledger_path())
        return 0
    if a_.cmd == "add":
        row = append({"op": "add", "id": a_.id, "kind": a_.kind, "title": a_.title[:200], "count": a_.count,
                      "sessions": a_.sessions, "evidence": [e for e in a_.evidence.split(",") if e][:12],
                      "source": a_.source})
        print(json.dumps(row))
        return 0
    if a_.cmd == "status":
        if a_.id not in fold():
            print(json.dumps({"ok": False, "reason": f"unknown id {a_.id}"}))
            return 1
        print(json.dumps(append({"op": "status", "id": a_.id, "to": a_.to, "reason": a_.reason, "ref": a_.ref})))
        return 0
    if a_.cmd == "show":
        st = fold()
        if a_.id:
            if a_.id not in st:
                return 1
            print(json.dumps(st[a_.id], indent=1, ensure_ascii=False))
            return 0
        items = [f for f in st.values() if (not a_.status or f["status"] == a_.status)
                 and (not a_.kind or f.get("kind") == a_.kind)]
        items.sort(key=lambda f: (-(f.get("sessions") or 0), f["id"]))
        print(json.dumps(items, indent=1, ensure_ascii=False))
        return 0
    if a_.cmd == "check":
        sims = similar(a_.title, a_.kind, a_.threshold)
        denied = [x for x in sims if x["status"] == "denied"]
        print(json.dumps({"similar": sims[:8], "previously_denied": denied[:5],
                          "verdict": "denied-before" if denied else ("duplicate" if sims else "new")}, indent=1))
        return 0
    if a_.cmd == "stats":
        st = fold()
        print(json.dumps({"path": str(ledger_path()), "findings": len(st),
                          "by_status": dict(collections.Counter(f["status"] for f in st.values())),
                          "by_kind": dict(collections.Counter(f.get("kind") for f in st.values()))}, indent=1))
        return 0
    return 2


if __name__ == "__main__":
    sys.exit(main())
