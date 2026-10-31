#!/usr/bin/env python3
"""seed_room.py — seed a pre-populated stage room from a harvested conversation seed.

Usage: seed_room.py <conversation.json> <rooms.json> [--title T] [--agent AGENT_ID] [--owner OWNER_ID]

A room is the stage's native container for a conversation-with-an-agent. This lands the operator's agent
history as a room the operator OPENS and is immediately *inside* — not a cold workspace. A room the operator opens and is immediately inside of. Additive + idempotent (room id is a content hash).  
re-running is a no-op for the same conversation
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
    title = opt("--title", "My conversation history")
    agent = opt("--agent", "identity_operator")
    owner = opt("--owner", "operator")

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
    rid = "sr" + hashlib.sha256((conv.get("source_url", "") + title).encode()).hexdigest()[:6]
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
        "agent": agent,                      # agent id for this room (default identity_operator; agent-from-history mints the real one)
        "source_session": conv.get("source_url", "claude-web"),
        "updated": time.time(),
        "feed": [], "board": [], "work_queue": [], "files": [], "artifacts": {},
        "prefs": {"seeded_from": "claude-web-history"},
    }
    rooms.append(room)
    json.dump(store, open(rooms_path, "w"), indent=1)
    print(f"seeded room {rid} '{title}' with {len(history)} turns, agent={agent}, owner={owner}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
