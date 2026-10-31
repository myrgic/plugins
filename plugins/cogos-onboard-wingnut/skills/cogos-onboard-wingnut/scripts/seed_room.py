#!/usr/bin/env python3
"""seed_room.py — seed a pre-populated stage room from a Wingnut pack.

Usage: seed_room.py <conversation.json> <rooms.json> [--title T] [--agent identity_wingnut] [--owner vroku]

A room is the stage's native container for a conversation-with-an-agent. This lands the Wingnut
history as a room Vroku OPENS and is immediately *inside* — not a cold workspace. Same move we did on
Chaz's stage (room 03e272), shipped to Vroku's. Additive + idempotent: skips if a wingnut-history room
already exists; never clobbers his other rooms.
"""
import hashlib
import json
import os
import secrets
import sys
import time


def main():
    if len(sys.argv) < 3:
        print("usage: seed_room.py <conversation.json> <rooms.json> [--title T] [--agent A] [--owner O]", file=sys.stderr)
        return 2
    conv_path, rooms_path = sys.argv[1], sys.argv[2]
    args = sys.argv[3:]
    def opt(flag, default):
        return args[args.index(flag) + 1] if flag in args else default
    title = opt("--title", "Wingnut — Oracles history")
    agent = opt("--agent", "identity_wingnut")
    owner = opt("--owner", "vroku")

    conv = json.load(open(conv_path))
    turns = conv.get("turns") or conv.get("messages") or []
    if not turns:
        print("no turns in conversation", file=sys.stderr)
        return 1

    # load or init the room store
    if os.path.exists(rooms_path):
        store = json.load(open(rooms_path))
    else:
        os.makedirs(os.path.dirname(rooms_path) or ".", exist_ok=True)
        store = {"rooms": []}
    rooms = store.setdefault("rooms", [])

    # idempotency: a content-addressed room id from the conversation, so re-seeding is a no-op
    rid = "wn" + hashlib.sha256((conv.get("source_url", "") + title).encode()).hexdigest()[:6]
    if any(r.get("id") == rid for r in rooms):
        print(f"room {rid} already seeded; no-op")
        return 0

    # build the room's history from the turns ("me" = the person, "cog" = the agent)
    history = []
    for i, t in enumerate(turns):
        kind = "me" if t.get("role") == "human" else "cog"
        history.append({
            "id": f"h-{i:04d}",
            "kind": kind,
            "text": t.get("text") or t.get("content") or "",
            "ts": None,                      # the export carries no per-turn timestamps
        })

    room = {
        "id": rid,
        "key": secrets.token_urlsafe(18),    # the room's own bearer key (he owns it)
        "title": title,
        "history": history,
        "owner": owner,
        "members": [owner],
        "agent": agent,                      # identity_wingnut — his default agent, seeded
        "source_session": conv.get("source_url", "wingnut-share"),
        "updated": time.time(),
        "feed": [], "board": [], "work_queue": [], "files": [], "artifacts": {},
        "prefs": {"seeded_from": "wingnut-oracles-collaboration"},
    }
    rooms.append(room)
    json.dump(store, open(rooms_path, "w"), indent=1)
    print(f"seeded room {rid} '{title}' with {len(history)} turns, agent={agent}, owner={owner}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
