#!/usr/bin/env python3
"""seed_observatory.py — normalize a Claude-share conversation export into a CogOS observatory index.

Usage: seed_observatory.py <export.json> <out_dir>

Writes one index file in the observatory's `<source>__<session>.json` shape (a JSON array of turn
records), source `claude-web`, each turn carrying refs.stable_id — so the operator's Cog can
retrieve the shared history from turn one. Additive: writes a new source file, never prunes.
"""
import hashlib
import json
import os
import sys


def main():
    if len(sys.argv) != 3:
        print("usage: seed_observatory.py <export.json> <out_dir>", file=sys.stderr)
        return 2
    export_path, out_dir = sys.argv[1], sys.argv[2]
    with open(export_path) as f:
        data = json.load(f)
    turns = data.get("turns") or data.get("messages") or (data if isinstance(data, list) else [])
    if not turns:
        print("no turns found in export", file=sys.stderr)
        return 1

    # a stable session id derived from the export (the share id if present, else a content hash)
    src_url = data.get("source_url", "")
    session = "".join(c for c in src_url.split("/")[-1] if c.isalnum())[:24] or \
        hashlib.sha256(export_path.encode()).hexdigest()[:16]
    source = "claude-web"

    role_map = {"human": "user", "assistant": "assistant"}
    records = []
    for i, t in enumerate(turns):
        role = role_map.get(t.get("role", ""), t.get("role", "user"))
        text = t.get("text") or t.get("content") or ""
        # stable_id: content-addressed so re-seeding is idempotent (not keyed on volatile index)
        sid = f"{source}:{hashlib.sha256((session + str(i) + text[:200]).encode()).hexdigest()[:16]}"
        records.append({
            "identity": {"stable_id": sid},
            "refs": {"stable_id": sid},
            "role": role,
            "schema": "observatory.v1",
            "session_id": f"{source}__{session}",
            "source": source,
            "text": text,
            "timestamp": None,          # the export carries no per-turn timestamps
            "session_title": data.get("subject", "My session history"),
        })

    os.makedirs(out_dir, exist_ok=True)
    out_path = os.path.join(out_dir, f"{source}__{session}.json")
    with open(out_path, "w") as f:
        json.dump(records, f, indent=1)
    print(f"seeded {len(records)} turns -> {out_path}")
    print(f"source={source} session={session}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
