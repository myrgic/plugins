#!/usr/bin/env python3
"""harvest_claude_web.py — harvest YOUR OWN claude.ai web conversation history.

Drives a browser YOU are logged into to claude.ai over the Chrome DevTools Protocol (CDP),
runs the claude.ai web API from inside that tab (so your cookies ride along and Cloudflare
passes), and normalizes every conversation into:

  <out>/conversations/<uuid>.json   per-conversation record, shaped like the conversation seed consumed by seed_observatory.py / seed_room.py
                                    ({source_url, subject, turns:[{role,text,ts}]}) so
                                    seed_observatory.py / seed_room.py can consume it directly
  <out>/observatory/claude-web__<session>.json
                                    observatory index: one JSON array of turn records per
                                    conversation, each with a content-addressed refs.stable_id
                                    (re-running is idempotent)
  <out>/rooms.json                  stage-room seed store (same shape seed_room.py writes),
                                    one room per conversation; skipped/updated idempotently
  <out>/harvest_manifest.json       counts, skipped items, errors (no message text)

PRIVACY: this script only talks to the browser endpoint you point it at (default
http://127.0.0.1:9222 on YOUR machine). It holds no credentials, reads no cookie store, and
never writes anything outside --out. Nothing is uploaded anywhere.

Requirements: Python 3.8+ standard library only. A Chromium-based browser (Chrome, Edge,
Brave, BrowserOS) started with remote debugging, and claude.ai logged in in that browser.
"""
import argparse
import base64
import hashlib
import json
import os
import secrets
import socket
import struct
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

CLAUDE_HOST = "claude.ai"
DEFAULT_CDP = "http://127.0.0.1:9222"

EXIT_OK = 0
EXIT_ERROR = 1
EXIT_USAGE = 2
EXIT_BLOCKED = 3  # logged out, or Cloudflare challenge that never cleared


class Blocked(Exception):
    """Logged out, or Cloudflare / login wall in the browser tab."""


class ApiError(Exception):
    """A claude.ai API call returned something unexpected for one item."""


# ── raw CDP over stdlib WebSocket (RFC 6455, client side) ───────────────────

def http_json(base, path, method="GET"):
    req = urllib.request.Request(base + path, method=method)
    with urllib.request.urlopen(req, timeout=10) as r:
        return json.loads(r.read().decode("utf-8") or "null")


def _recv_exact(sock, n):
    buf = b""
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        if not chunk:
            raise IOError("websocket closed by browser")
        buf += chunk
    return buf


def _ws_connect(host, port, path):
    key = base64.b64encode(os.urandom(16)).decode()
    sock = socket.create_connection((host, port), timeout=120)
    req = (f"GET {path} HTTP/1.1\r\nHost: {host}:{port}\r\nUpgrade: websocket\r\n"
           f"Connection: Upgrade\r\nSec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n\r\n")
    sock.sendall(req.encode())
    resp = b""
    while b"\r\n\r\n" not in resp:
        chunk = sock.recv(4096)
        if not chunk:
            raise IOError("websocket handshake closed")
        resp += chunk
    status_line = resp.split(b"\r\n", 1)[0]
    if b" 101 " not in status_line:
        raise IOError("websocket upgrade refused: %r" % (status_line[:120],))
    return sock


def _ws_send(sock, opcode, data):
    mask = os.urandom(4)
    hdr = bytearray([0x80 | opcode])
    n = len(data)
    if n < 126:
        hdr.append(0x80 | n)
    elif n < 65536:
        hdr.append(0x80 | 126)
        hdr += struct.pack(">H", n)
    else:
        hdr.append(0x80 | 127)
        hdr += struct.pack(">Q", n)
    hdr += mask
    sock.sendall(bytes(hdr) + bytes(b ^ mask[i % 4] for i, b in enumerate(data)))


def _ws_recv_json(sock):
    """Return the next complete text message as parsed JSON (handles fragments and pings)."""
    msg = b""
    while True:
        b1, b2 = _recv_exact(sock, 2)
        fin = b1 & 0x80
        opcode = b1 & 0x0F
        ln = b2 & 0x7F
        if ln == 126:
            ln = struct.unpack(">H", _recv_exact(sock, 2))[0]
        elif ln == 127:
            ln = struct.unpack(">Q", _recv_exact(sock, 8))[0]
        payload = _recv_exact(sock, ln)  # server frames are unmasked
        if opcode == 0x8:
            raise IOError("websocket closed by browser")
        if opcode == 0x9:  # ping -> pong
            _ws_send(sock, 0xA, payload)
            continue
        if opcode == 0xA:  # unsolicited pong
            continue
        if opcode != 0:  # start of a new data frame
            msg = payload
        else:
            msg += payload
        if fin and opcode in (0x0, 0x1, 0x2):
            return json.loads(msg.decode("utf-8"))


class CDPSession:
    """One websocket to one page target; sequential Runtime.evaluate calls."""

    def __init__(self, ws_url):
        u = urllib.parse.urlparse(ws_url)
        self.sock = _ws_connect(u.hostname, u.port or 80,
                                u.path + (("?" + u.query) if u.query else ""))
        self._id = 0

    def call(self, method, params=None):
        self._id += 1
        my_id = self._id
        _ws_send(self.sock, 0x1, json.dumps(
            {"id": my_id, "method": method, "params": params or {}}).encode("utf-8"))
        while True:
            m = _ws_recv_json(self.sock)
            if m.get("id") == my_id:
                if "error" in m:
                    raise IOError(f"CDP {method} error: {m['error']}")
                return m.get("result", {})

    def evaluate(self, expression):
        r = self.call("Runtime.evaluate", {
            "expression": expression,
            "returnByValue": True,
            "awaitPromise": True,
        })
        if "exceptionDetails" in r:
            raise IOError("page-side exception during evaluate")
        return (r.get("result") or {}).get("value")

    def close(self):
        try:
            self.sock.close()
        except Exception:
            pass


# ── claude.ai access (runs INSIDE the operator's logged-in tab) ─────────────

# The fetch runs in the page so the operator's own cookies are sent. It returns a JSON
# envelope {status, ct, body} so Python can classify the response.
FETCH_JS = """(async (path) => {
  try {
    const r = await fetch(path, {credentials: 'include', headers: {'accept': 'application/json'}});
    const body = await r.text();
    return JSON.stringify({status: r.status, ct: r.headers.get('content-type') || '', body: body});
  } catch (e) {
    return JSON.stringify({status: 0, ct: '', body: '', error: String(e)});
  }
})"""


def api_get(cdp, path):
    raw = cdp.evaluate(FETCH_JS + "(" + json.dumps(path) + ")")
    env = json.loads(raw) if raw else {"status": 0, "body": ""}
    status, body = env.get("status", 0), env.get("body", "")
    if status == 200:
        try:
            return json.loads(body)
        except ValueError:
            raise Blocked("200 but non-JSON body (Cloudflare challenge or login page)")
    if status in (401, 403) or "Just a moment" in body or "cf-chl" in body:
        raise Blocked(f"HTTP {status} from claude.ai (logged out or Cloudflare challenge)")
    raise ApiError(f"HTTP {status} {env.get('error', '')} on {path.split('?')[0]}")


def wait_ready(cdp, timeout=90):
    t0 = time.time()
    announced = False
    while True:
        try:
            info = json.loads(cdp.evaluate(
                "JSON.stringify({ready: document.readyState, title: document.title, "
                "host: location.host})") or "{}")
        except (IOError, ValueError):
            info = {}
        if (info.get("host", "").endswith(CLAUDE_HOST) and info.get("ready") == "complete"
                and "Just a moment" not in info.get("title", "")):
            return
        if time.time() - t0 > timeout:
            raise Blocked(f"claude.ai tab not ready (host={info.get('host')!r}, "
                          f"title={info.get('title')!r}). Solve any Cloudflare check or log in "
                          f"in that browser tab, then re-run.")
        if not announced:
            print("waiting for the claude.ai tab to be ready ...")
            announced = True
        time.sleep(2)


def get_orgs(cdp):
    boot = api_get(cdp, "/api/bootstrap")
    acct = boot.get("account") if isinstance(boot, dict) else None
    if not acct:
        raise Blocked("bootstrap returned no account: the browser is not logged into claude.ai")
    orgs = {}
    for m in acct.get("memberships") or []:
        o = (m or {}).get("organization") or {}
        if o.get("uuid"):
            orgs[o["uuid"]] = o.get("name") or o["uuid"]
    if not orgs:
        raise Blocked("logged in, but no organization memberships found")
    return orgs


def list_conversations(cdp, org, page_size):
    """Paginate the conversation list. Stops on an empty page or a page with no new uuids,
    so it is correct even if the server caps page size below the requested limit."""
    out, seen, offset = [], set(), 0
    while True:
        page = api_get(cdp, f"/api/organizations/{org}/chat_conversations"
                            f"?limit={page_size}&offset={offset}")
        if not isinstance(page, list):
            raise ApiError("conversation list was not an array")
        new = 0
        for c in page:
            u = (c or {}).get("uuid")
            if u and u not in seen:
                seen.add(u)
                out.append(c)
                new += 1
        if not page or new == 0:
            break
        offset += len(page)
    return out


def get_conversation_turns(cdp, org, uuid):
    d = api_get(cdp, f"/api/organizations/{org}/chat_conversations/{uuid}"
                     f"?tree=True&rendering_mode=messages")
    msgs = d.get("chat_messages") if isinstance(d, dict) else None
    if msgs is None:
        raise ApiError("response had no chat_messages")
    turns = []
    for m in msgs:
        role = {"human": "human", "assistant": "assistant"}.get((m or {}).get("sender"))
        if not role:
            continue
        text = m.get("text") or ""
        if not text:
            blocks = m.get("content")
            if isinstance(blocks, str):
                text = blocks
            elif isinstance(blocks, list):
                text = "\n".join(b.get("text", "") for b in blocks
                                 if isinstance(b, dict) and b.get("type") == "text")
        text = (text or "").strip()
        if text:
            turns.append({"role": role, "text": text, "ts": m.get("created_at")})
    return turns


# ── tab selection (never navigates your existing tab) ───────────────────────

def ws_for_target(base, target):
    bu = urllib.parse.urlparse(base)
    path = urllib.parse.urlparse(target["webSocketDebuggerUrl"]).path
    return f"ws://{bu.hostname}:{bu.port or 80}{path}"


def pick_claude_tab(base):
    """Use an existing claude.ai tab if one is open. Otherwise open our own tab and return
    its id so we close it afterwards. Returns (ws_url, created_tab_id_or_None)."""
    pages = [t for t in http_json(base, "/json/list")
             if t.get("type") == "page" and t.get("webSocketDebuggerUrl")]
    hits = [t for t in pages if CLAUDE_HOST in t.get("url", "")]
    if hits:
        return ws_for_target(base, hits[0]), None
    try:
        t = http_json(base, "/json/new?https://claude.ai/", method="PUT")
    except urllib.error.HTTPError:
        t = http_json(base, "/json/new?https://claude.ai/")
    return ws_for_target(base, t), t["id"]


def close_created_tab(base, tab_id):
    try:
        urllib.request.urlopen(f"{base}/json/close/{tab_id}", timeout=10).read()
    except Exception:
        pass


# ── normalization (shapes mirror seed_observatory.py / seed_room.py) ────────

def sha16(s):
    return hashlib.sha256(s.encode("utf-8")).hexdigest()[:16]


def atomic_write_json(path, obj):
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, indent=1, ensure_ascii=False)
    os.replace(tmp, path)


def write_observatory(obs_dir, session, title, source_url, turns):
    source = "claude-web"
    records = []
    for i, t in enumerate(turns):
        # content-addressed: identical content re-harvested gives identical stable_ids
        sid = f"{source}:{sha16(session + str(i) + t['text'][:200])}"
        records.append({
            "identity": {"stable_id": sid},
            "refs": {"stable_id": sid},
            "role": "user" if t["role"] == "human" else "assistant",
            "schema": "observatory.v1",
            "session_id": f"{source}__{session}",
            "source": source,
            "text": t["text"],
            "timestamp": t.get("ts"),
            "session_title": title,
            "source_url": source_url,
        })
    atomic_write_json(os.path.join(obs_dir, f"{source}__{session}.json"), records)


def upsert_room(store, source_url, title, turns, updated_at, owner, agent):
    """Idempotent: same room id per conversation. Returns 'created' | 'updated' | 'unchanged'."""
    rid = "cw" + sha16(source_url)[:6]
    history = [{"id": f"h-{i:04d}", "kind": "me" if t["role"] == "human" else "cog",
                "text": t["text"], "ts": t.get("ts")} for i, t in enumerate(turns)]
    rooms = store.setdefault("rooms", [])
    existing = next((r for r in rooms if r.get("id") == rid), None)
    if existing:
        prefs = existing.setdefault("prefs", {})
        if prefs.get("source_updated_at") == updated_at and existing.get("history") == history:
            return "unchanged"
        existing["title"] = title
        existing["history"] = history
        existing["updated"] = time.time()
        prefs["source_updated_at"] = updated_at
        return "updated"
    rooms.append({
        "id": rid,
        "key": secrets.token_urlsafe(18),  # the room's own bearer key, generated locally
        "title": title,
        "history": history,
        "owner": owner,
        "members": [owner],
        "agent": agent,
        "source_session": source_url,
        "updated": time.time(),
        "feed": [], "board": [], "work_queue": [], "files": [], "artifacts": {},
        "prefs": {"seeded_from": "claude-web-history", "source_updated_at": updated_at},
    })
    return "created"


def load_json(path, default):
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    return default


# ── main ────────────────────────────────────────────────────────────────────

def main(argv=None):
    ap = argparse.ArgumentParser(
        description="Harvest your own claude.ai web conversations via your logged-in browser "
                    "(CDP) into observatory + stage-room seed files.")
    ap.add_argument("--cdp", default=DEFAULT_CDP,
                    help=f"CDP HTTP endpoint of YOUR browser (default {DEFAULT_CDP})")
    ap.add_argument("--out", default="claude-web-harvest", help="output directory")
    ap.add_argument("--org", default=None,
                    help="only this organization (uuid or name substring); default: all orgs")
    ap.add_argument("--limit", type=int, default=0,
                    help="fetch at most N conversations in total (0 = all); for testing")
    ap.add_argument("--count-only", action="store_true",
                    help="list conversations per org and stop (no message fetch)")
    ap.add_argument("--no-rooms", action="store_true", help="skip stage-room seed output")
    ap.add_argument("--owner", default="operator", help="room owner id (default: operator)")
    ap.add_argument("--agent", default="identity_operator",
                    help="agent id seeded into rooms (default identity_operator)")
    ap.add_argument("--page-size", type=int, default=50, help="conversation list page size")
    ap.add_argument("--delay", type=float, default=0.25,
                    help="seconds between conversation fetches (be gentle)")
    args = ap.parse_args(argv)

    base = args.cdp.rstrip("/")
    out = args.out
    conv_dir = os.path.join(out, "conversations")
    obs_dir = os.path.join(out, "observatory")
    rooms_path = os.path.join(out, "rooms.json")
    manifest = {"started": time.strftime("%Y-%m-%dT%H:%M:%S%z"), "cdp": base,
                "orgs": {}, "conversations_listed": 0, "fetched": 0, "reused": 0,
                "empty": 0, "errors": [], "rooms": {"created": 0, "updated": 0, "unchanged": 0}}
    cdp = None
    created_tab = None
    code = EXIT_OK

    try:
        try:
            ws_url, created_tab = pick_claude_tab(base)
        except (OSError, ValueError) as e:
            print(f"cannot reach a CDP endpoint at {base}: {e}\n"
                  f"start your browser with remote debugging, e.g.\n"
                  f"  chrome --remote-debugging-port=9222   (and log into claude.ai in it)",
                  file=sys.stderr)
            return EXIT_BLOCKED

        cdp = CDPSession(ws_url)
        wait_ready(cdp)

        orgs = get_orgs(cdp)
        if args.org:
            orgs = {u: n for u, n in orgs.items()
                    if args.org == u or args.org.lower() in n.lower()}
            if not orgs:
                print(f"no organization matches --org {args.org!r}", file=sys.stderr)
                return EXIT_USAGE
        manifest["orgs"] = orgs

        convs = []
        for org, oname in orgs.items():
            lst = list_conversations(cdp, org, args.page_size)
            print(f"org {oname!r} ({org[:8]}): {len(lst)} conversations")
            convs.extend((org, oname, c) for c in lst)
        manifest["conversations_listed"] = len(convs)
        print(f"total conversations listed: {len(convs)}")
        if args.count_only:
            return EXIT_OK
        if args.limit and args.limit > 0:
            convs = convs[:args.limit]
            print(f"--limit: processing first {len(convs)}")

        rooms_store = {"rooms": []} if args.no_rooms else load_json(rooms_path, {"rooms": []})
        consecutive_blocked = 0

        for idx, (org, oname, c) in enumerate(convs, 1):
            uuid = c["uuid"]
            name = c.get("name") or "(untitled)"
            updated = c.get("updated_at")
            conv_path = os.path.join(conv_dir, f"{uuid}.json")
            source_url = f"https://claude.ai/chat/{uuid}"
            title = f"Claude web — {name}"
            try:
                # resumable: reuse a saved conversation whose updated_at has not changed
                prev = load_json(conv_path, None) if os.path.exists(conv_path) else None
                if prev and prev.get("updated_at") == updated and prev.get("turns"):
                    turns = prev["turns"]
                    manifest["reused"] += 1
                else:
                    turns = get_conversation_turns(cdp, org, uuid)
                    consecutive_blocked = 0
                    manifest["fetched"] += 1
                    time.sleep(args.delay)
                if not turns:
                    manifest["empty"] += 1
                    continue
                atomic_write_json(conv_path, {
                    "source": "claude-web", "source_url": source_url, "subject": title,
                    "uuid": uuid, "org": org, "org_name": oname, "name": name,
                    "created_at": c.get("created_at"), "updated_at": updated,
                    "turns": turns,
                })
                session = uuid.replace("-", "")
                write_observatory(obs_dir, session, title, source_url, turns)
                if not args.no_rooms:
                    r = upsert_room(rooms_store, source_url, title, turns, updated,
                                    args.owner, args.agent)
                    manifest["rooms"][r] += 1
            except Blocked as e:
                consecutive_blocked += 1
                manifest["errors"].append({"uuid": uuid, "error": str(e)})
                print(f"[{idx}/{len(convs)}] blocked: {e}")
                if consecutive_blocked >= 3:
                    raise
            except (ApiError, IOError, ValueError, KeyError) as e:
                manifest["errors"].append({"uuid": uuid, "error": str(e)})
                print(f"[{idx}/{len(convs)}] skipped {uuid[:8]}: {e}")

            if idx % 25 == 0:
                print(f"progress {idx}/{len(convs)}")
                if not args.no_rooms:
                    atomic_write_json(rooms_path, rooms_store)

        if not args.no_rooms:
            atomic_write_json(rooms_path, rooms_store)
        print(f"done: fetched={manifest['fetched']} reused={manifest['reused']} "
              f"empty={manifest['empty']} errors={len(manifest['errors'])} "
              f"rooms={manifest['rooms']}")
        if manifest["errors"]:
            code = EXIT_ERROR

    except Blocked as e:
        msg = f"BLOCKED: {e}"
        print(msg, file=sys.stderr)
        manifest["errors"].append({"fatal": str(e)})
        code = EXIT_BLOCKED
    except (OSError, IOError) as e:
        print(f"connection problem: {e}", file=sys.stderr)
        manifest["errors"].append({"fatal": str(e)})
        code = EXIT_ERROR
    finally:
        if cdp:
            cdp.close()
        if created_tab:
            close_created_tab(base, created_tab)
        manifest["finished"] = time.strftime("%Y-%m-%dT%H:%M:%S%z")
        try:
            atomic_write_json(os.path.join(out, "harvest_manifest.json"), manifest)
        except OSError:
            pass
    return code


if __name__ == "__main__":
    sys.exit(main())
