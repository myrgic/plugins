#!/usr/bin/env python3
"""seat-identity-heal.py — SessionStart self-heal for a durable seat role.

Problem: a session's durable role (e.g. a standing seat name distinct from
the harness's default) can get silently clobbered if any other SessionStart
handler re-registers the session with a generic role. /v1/sessions/register
on the CogOS kernel is a full-row replace, not a field merge, so the last
POST to land within a given SessionStart dispatch wins the role field.
Transient session fields self-heal at the next heartbeat (a partial update);
role has no such self-heal path. This hook is that path.

Fix: read a durable identity pointer at ~/.cog/status/seat-identity.json
(written once, out of band — NOT by this hook) and re-assert it against the
live kernel registry via POST /v1/sessions/register — the exact REST
counterpart of the cog_register_session MCP tool.

Contract: never raises, always exits 0, no dependency on stdin content beyond
a best-effort hook-event-name echo, short local-HTTP timeout so the hook
stays within its latency budget as a bounded best-effort, not a guarantee
tied to real work. Missing/corrupt identity file -> fully silent no-op (no
stdout at all): absence of signal, not an alarm. No cog workspace / no
identity file at all (the common case for anyone who hasn't set up a
standing seat) also degrades to silence.
"""
from __future__ import annotations

import json
import os
import socket
import sys
import urllib.error
import urllib.request
from pathlib import Path

IDENTITY_PATH = Path.home() / ".cog" / "status" / "seat-identity.json"
# COGOS_KERNEL_URL takes precedence when set; COGOS_KERNEL_PORT is a
# localhost-only convenience default. Same precedence as
# kernel-vitals-probe.py so both hooks resolve to the same kernel.
KERNEL_URL = os.environ.get("COGOS_KERNEL_URL") or \
    f"http://127.0.0.1:{os.environ.get('COGOS_KERNEL_PORT', '6931')}"
TIMEOUT = 1.0  # seconds; localhost-only call, kept short per the hook budget
GRANT_TIMEOUT = 1.0

# v0.16.29: kernel writes require an X-Cogos-Grant header. Resolved once per
# process and cached: vault file first, loopback grants/current GET as
# fallback. Any acquisition failure means "proceed without the header" --
# fail-open, matching this hook's own silent-no-op contract. A broken vault
# must never break the heal; the 401 that follows routes to the existing
# fallback additionalContext exactly as a kernel-down response would.
_GRANT_CACHE: dict = {"tried": False, "token": None}


def _get_grant() -> str | None:
    if _GRANT_CACHE["tried"]:
        return _GRANT_CACHE["token"]
    _GRANT_CACHE["tried"] = True
    token = None
    try:
        raw = (Path.home() / ".cog" / "vault" / "node-root-grant").read_text(encoding="utf-8")
        token = raw.strip() or None
    except Exception:
        token = None
    if not token:
        try:
            with urllib.request.urlopen(
                f"{KERNEL_URL}/v1/identity/grants/current?surface=node-root",
                timeout=GRANT_TIMEOUT,
            ) as r:
                if r.status == 200:
                    token = (json.loads(r.read()).get("token")) or None
        except Exception:
            token = None
    _GRANT_CACHE["token"] = token
    return token


def _load_identity() -> dict | None:
    """Read + validate the durable identity file. Returns None on any
    problem (missing, unreadable, corrupt JSON, missing required fields) —
    callers must treat None as "say nothing, exit 0"."""
    try:
        raw = IDENTITY_PATH.read_text(encoding="utf-8")
        data = json.loads(raw)
    except Exception:
        return None
    if not isinstance(data, dict):
        return None
    if not data.get("session_id") or not data.get("role") or not data.get("workspace"):
        return None
    return data


def _register(identity: dict) -> dict | None:
    """POST the idempotent re-registration to the live kernel. Returns the
    parsed JSON response on HTTP 200, else None (kernel down, non-200,
    unparseable body, timeout) — any of which routes the caller to the
    fallback additionalContext."""
    body = {
        "session_id": identity["session_id"],
        "workspace": identity["workspace"],
        "role": identity["role"],
    }
    if identity.get("hostname"):
        body["hostname"] = identity["hostname"]
    headers = {"Content-Type": "application/json"}
    grant = _get_grant()
    if grant:
        headers["X-Cogos-Grant"] = grant
    req = urllib.request.Request(
        f"{KERNEL_URL}/v1/sessions/register",
        data=json.dumps(body).encode(),
        headers=headers,
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
            if r.status != 200:
                return None
            return json.loads(r.read())
    except (urllib.error.URLError, socket.timeout, json.JSONDecodeError, OSError, ValueError):
        return None


def main() -> None:
    try:
        evt = "SessionStart"
        try:
            raw = sys.stdin.read()
            if raw.strip():
                evt = json.loads(raw).get("hook_event_name") or evt
        except Exception:
            pass

        identity = _load_identity()
        if identity is None:
            return  # missing/corrupt identity file: silent no-op, per contract

        role = identity["role"]
        sid = identity["session_id"]
        result = _register(identity)

        if result is not None and result.get("ok"):
            context = (
                f'<seat_identity role="{role}" healed="rest">'
                f're-asserted session {sid} as role="{role}" on the kernel '
                f'registry (POST /v1/sessions/register, seq={result.get("seq")})'
                f"</seat_identity>"
            )
        else:
            context = (
                f'<seat_identity role="{role}">compaction may have reset the '
                f"registry role; re-assert via cog_register_session</seat_identity>"
            )

        print(json.dumps({
            "hookSpecificOutput": {
                "hookEventName": evt,
                "additionalContext": context,
            }
        }))
    except Exception:
        pass


if __name__ == "__main__":
    main()
    sys.exit(0)
