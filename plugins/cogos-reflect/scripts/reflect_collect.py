#!/usr/bin/env python3
"""reflect_collect.py: build a redacted evidence bundle from agent session history.

Reads (whatever exists, all read-only):
  claude-code   ~/.claude/projects/*/*.jsonl  (+ extra roots via --claude-root)
  codex         ~/.codex/sessions/**/rollout-*.jsonl (+ --codex-root)
  hermes        <hermes home>/profiles/*/state.db and <hermes home>/state.db (sqlite, read-only URI)
  observatory   <cog workspace>/.cog/state/conversations/*.json  (CogOS nodes; text-only sources)

Writes into the run dir:
  bundle.json     compact cross-session evidence (the only file the model should read)
  sessions.jsonl  one redacted line per session, for drill-down by id
Prints a short JSON summary on stdout. Exit 0 ok, 3 no sessions found, 2 usage error.
"""
from __future__ import annotations

import argparse
import collections
import glob
import json
import os
import re
import sqlite3
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from reflect_common import (  # noqa: E402
    SCHEMA_VERSION, content_words, epoch_to_iso, find_cog_workspace, iso_to_epoch, jaccard,
    new_run_dir, parse_since, quote, redact, stable_id, write_json,
)

# ----------------------------------------------------------------------------- operator text

# Harness-injected user-role text that is not the operator speaking. Prefix match after strip.
INJECTED_PREFIXES = (
    "[since your last turn]", "[stage", "[work thread", "[speech]", "[CONTEXT COMPACTION",
    "[People on the stage", "[System", "[Your active task list", "You've reached the maximum",
    "[IMPORTANT: You are running as a scheduled", "[IMPORTANT:", "\x00json:", "<system-reminder>",
    "<command-name>", "<local-command", "Caveat: The messages below", "[Request interrupted",
    "[Recent channel messages]", "This session is being continued", "<task-notification>",
    "Base directory for this skill", "[SYSTEM", "[Chaz moved to", "[Image", "[Attached",
    "Seat transcript tail", "(The user sent a message with no text content)", "[digest",
    "[Cog's heartbeat]", "[Messages prefixed with", "[managed lane", "[lane ", "[cron",
)
SPEAKER_TAG = re.compile(r"^\[(?!screenshot\]|image\]|file\])(?:[A-Za-z][\w .\-]{0,40}|typed[^\]]{0,20}|spoken|from [^\]]{1,40})\]\s*")
TIMESTAMP_TAG = re.compile(r"^\[(?:Mon|Tue|Wed|Thu|Fri|Sat|Sun) \d{4}-\d\d-\d\d [^\]]{0,20}\]\s*")

CORRECTION = re.compile(
    r"(?i)(^no[,.! ]|^nope\b|\bdon'?t\b|\bdo not\b|\bstop\b|\bnever\b|\bnot what i\b|\bi said\b|\bi told you\b|"
    r"\bwhy did you\b|\bwhy are you\b|\byou forgot\b|\bagain\b|\bstill\b.*\b(broken|wrong|failing|not)\b|"
    r"\bwrong\b|\binstead\b|\bplease (don'?t|stop|always|never)\b|\balways\b|\bshould(n'?t| not)? have\b|"
    r"\bthat'?s not\b|\bundo\b|\brevert\b|\byou keep\b|\bkeep (doing|forgetting)\b)"
)


# Harness annotations embedded INSIDE a user turn. Removed before analysis.
EMBEDDED_BLOCK = re.compile(
    r"\[(?:Available tools|Skill directory|Attached image|Image attached at|Context from the interrupted|"
    r"This response was interrupted|People on the stage now|Chaz moved to|Meeting mode|Meeting ended|NOTE|"
    r"The user sent a|Chaz tapped Respond|Voice channel now|Replying to|Attached file|Document attached)"
    r"[^\]]*\]", re.I)
# Text that, wherever it appears, means the turn is a delegated brief / harness nudge, not the operator.
BRIEF_MARKERS = re.compile(
    r"^(?:\[stage work thread\]|You are (?:Cog's|reviewing|a |an |the |drafting|evaluating|running|working)|"
    r"You stopped before finishing|You were blocked for over|Your previous turn indicated|You just executed tool calls|"
    r"The stage's deterministic|Continue the task|Reply with (?:exactly|the single word)|\u270d note:)", re.I)


def _strip_injected(t: str) -> str:
    # Auto-loaded skill bodies: keep only what follows the skill's closing boilerplate.
    if "[Skill directory:" in t:
        tail = re.split(r"\[Skill directory:[^\]]*\]\s*(?:Resolve any relative paths[^\n]*\n)?", t)[-1]
        t = tail.strip()
    if "[Recent channel messages]" in t:
        parts = re.split(r"\[Now addressed to you:?[^\]]*\]", t)
        t = parts[-1] if len(parts) > 1 else re.sub(r"\[Recent channel messages\][\s\S]*?(\n\n|$)", "", t)
    marker = "User correction/guidance after interrupt:"
    if marker in t:
        t = t.rsplit(marker, 1)[1]
    t = EMBEDDED_BLOCK.sub(" ", t)
    t = re.sub(r"(?m)^URI: file:\S+\s*$", "", t)
    return t.strip()


def operator_texts(raw, operators: list[str] | None) -> list[tuple[str, str]]:
    """Split a user-role message into real operator utterances: [(text, speaker)]."""
    if not raw or not isinstance(raw, str):
        return []
    out = []
    for chunk in re.split(r"\[New message\]", raw):
        t = TIMESTAMP_TAG.sub("", chunk.strip())
        if t.startswith(INJECTED_PREFIXES):
            continue
        speaker = ""
        m = SPEAKER_TAG.match(t)
        if m:
            speaker = m.group(0).strip()[1:-1]
            t = TIMESTAMP_TAG.sub("", t[m.end():].strip())
        t = _strip_injected(t)
        if not t or t.startswith(INJECTED_PREFIXES):
            continue
        m2 = SPEAKER_TAG.match(t)  # speaker tag that only surfaced after stripping
        if m2 and not speaker:
            speaker = m2.group(0).strip()[1:-1]
            t = t[m2.end():].strip()
        if not t or BRIEF_MARKERS.match(t) or t.startswith(INJECTED_PREFIXES):
            continue
        if not re.sub(r"https?://\S+|\[screenshot\]", "", t).strip():
            continue  # link- or image-only
        if operators and speaker and not any(o.lower() in speaker.lower() for o in operators) \
                and not re.match(r"(typed|spoken)\b", speaker):
            continue  # another participant in a multi-user chat
        out.append((t[:6000], speaker))
    return out


def is_brief(raw) -> bool:
    """First user message of a delegated / automated session (a brief, not a person)."""
    if not raw or not isinstance(raw, str):
        return False
    t = TIMESTAMP_TAG.sub("", raw.strip())
    return bool(BRIEF_MARKERS.match(t)) or (len(t) > 1500 and t.startswith(("You are", "# ", "## ", "<")))


# ----------------------------------------------------------------------------- tool families

def bash_shape(cmd: str) -> str:
    """Normalize a shell command to a reusable 'shape': first command + subcommand + flags."""
    c = (cmd or "").strip()
    c = re.sub(r"^(cd\s+\S+\s*(&&|;)\s*)+", "", c)
    prev = None
    while prev != c:  # wrappers and env assignments, in any order
        prev = c
        c = re.sub(r"^((sudo|timeout\s+\d+\w?|env|nohup|time)\s+)+", "", c)
        c = re.sub(r"^([A-Z_][A-Z0-9_]*=\S+\s+)+", "", c)
    first = re.split(r"\s*(\|\||&&|\||;)\s*", c, maxsplit=1)[0]
    toks = first.split()
    if not toks:
        return ""
    head = os.path.basename(toks[0])
    shape = [head]
    for tk in toks[1:4]:
        if tk.startswith("-"):
            shape.append(tk.split("=")[0])
        elif re.fullmatch(r"[a-z][a-z0-9\-]{1,20}", tk) and len(shape) < 3:
            shape.append(tk)
        else:
            break
    return " ".join(shape)


def tool_family(name: str, args: dict | None) -> str:
    n = (name or "").strip()
    if n in ("Bash", "terminal", "PowerShell", "shell", "exec_command", "local_shell", "container.exec"):
        cmd = (args or {}).get("command") or (args or {}).get("cmd") or ""
        if isinstance(cmd, list):
            cmd = " ".join(map(str, cmd))
        s = bash_shape(str(cmd))
        return f"shell:{s}" if s else "shell"
    if n.startswith("mcp__"):
        parts = n.split("__")
        return f"mcp:{parts[1] if len(parts) > 1 else n}"
    return n or "unknown"


def skill_name_from_call(name: str, args: dict | None) -> str | None:
    a = args or {}
    if name == "Skill":
        return str(a.get("skill") or a.get("name") or "") or None
    if name in ("skill_view", "skills_view"):
        return str(a.get("name") or "") or None
    return None


# ----------------------------------------------------------------------------- session model

def new_session(sid: str, source: str) -> dict:
    return {
        "id": sid, "source": source, "start": None, "end": None, "title": "", "cwd": "",
        "operator_turns": [], "tool_calls": 0, "tool_errors": 0, "denied": 0,
        "families": collections.Counter(), "family_errors": collections.Counter(),
        "commands": [], "skills": collections.Counter(), "slow_calls": [], "automated": False,
        "_seen_user": False, "error_texts": {}, "error_sigs": collections.Counter(),
    }


def err_signature(gist: str) -> str:
    """Stable key for 'the same error': digits, paths, quoted bits and ids abstracted, first ~70 chars."""
    g = re.sub(r'^[{"\s]*(error|output)["\s:]*', "", gist or "")
    g = re.sub(r"(/[\w.\-]+)+", "<path>", g)
    g = re.sub(r"'[^']*'|`[^`]*`", "<q>", g)
    g = re.sub(r"\b[0-9a-f]{6,}\b|\d+", "N", g)
    return re.sub(r"\s+", " ", g).strip()[:70]


def err_gist(text: str) -> str:
    """The informative part of a tool error: the error field / last traceback line, redacted."""
    t = text or ""
    m = re.search(r'"error":\s*"((?:[^"\\]|\\.){4,300})"', t)
    if m:
        t = m.group(1)
    else:
        lines = [l for l in re.split(r"\\n|\n", t) if l.strip()]
        errs = [l for l in lines if re.search(r"(?i)error|exception|not found|denied|failed|no such", l)]
        t = (errs[-1] if errs else (lines[-1] if lines else t))
    return quote(t, 160)


OPERATORS: list[str] = []  # set from --operator; empty = every human speaker counts


def add_turn(s: dict, ts: float | None, raw: str):
    if not s["_seen_user"]:
        s["_seen_user"] = True
        if is_brief(raw):
            s["automated"] = True
            return
    for text, speaker in operator_texts(raw, OPERATORS):
        s["operator_turns"].append({"ts": ts, "text": text, "speaker": speaker})


def touch(s: dict, ts: float | None):
    if ts is None:
        return
    s["start"] = ts if s["start"] is None else min(s["start"], ts)
    s["end"] = ts if s["end"] is None else max(s["end"], ts)


SEEN_CALLS: set = set()  # tool-call ids are globally unique; replayed history repeats them


def add_call(s: dict, name: str, args: dict | None, ts: float | None, call_id: str | None = None):
    if call_id:
        if call_id in SEEN_CALLS:
            return None
        SEEN_CALLS.add(call_id)
    s["tool_calls"] += 1
    fam = tool_family(name, args)
    s["families"][fam] += 1
    sk = skill_name_from_call(name, args)
    if sk:
        s["skills"][sk] += 1
    if fam.startswith("shell"):
        cmd = (args or {}).get("command") or (args or {}).get("cmd") or ""
        if isinstance(cmd, list):
            cmd = " ".join(map(str, cmd))
        if cmd:
            s["commands"].append(str(cmd)[:400])
    return fam


DENIED = re.compile(r"(?i)(denied by|permission denied for tool|user (rejected|denied)|approval denied|"
                    r"blocked by (hook|policy)|requires approval|not allowed)")


def add_result(s: dict, fam: str | None, is_error: bool, text: str, dur: float | None):
    if is_error:
        s["tool_errors"] += 1
        if fam:
            s["family_errors"][fam] += 1
            if text:
                g = err_gist(text)
                if len(s["error_texts"].get(fam, [])) < 3:
                    s["error_texts"].setdefault(fam, []).append(g)
                s["error_sigs"][err_signature(g)] += 1
    if text and DENIED.search(text[:400]):
        s["denied"] += 1
    if dur is not None and dur > 60 and fam:
        s["slow_calls"].append((fam, round(dur, 1)))


def norm_command(cmd: str) -> str:
    """One command (first pipeline stage, cd-prefix stripped) with literals abstracted."""
    c = re.sub(r"^(cd\s+\S+\s*(&&|;)\s*)+", "", (cmd or "").strip())
    c = re.split(r"\s*(?:\|\||&&|;|\n)\s*", c, maxsplit=1)[0]
    c = c.split(" | ")[0]
    c = re.sub(r"'[^']*'|\"[^\"]*\"", "S", c)
    c = re.sub(r"\b[0-9a-f]{6,}\b", "H", c)
    c = re.sub(r"\d+", "N", c)
    return re.sub(r"\s+", " ", c).strip()[:240]


WAIT_FAMILIES = re.compile(r"^(shell:sleep|process|shell:timeout|wait|shell:watch)")


# ----------------------------------------------------------------------------- adapters

SCAN_WSL = os.environ.get("REFLECT_SCAN_WSL", "1") != "0"


def claude_roots(extra: list[str]) -> list[Path]:
    roots = [Path.home() / ".claude" / "projects"]
    if SCAN_WSL and Path("/mnt/c/Users").is_dir():  # WSL: Windows-side Claude Code
        roots += [Path(p) for p in glob.glob("/mnt/c/Users/*/.claude/projects")]
    roots += [Path(e).expanduser() for e in extra]
    seen, out = set(), []
    for r in roots:
        if r.is_dir() and r.resolve() not in seen:
            seen.add(r.resolve())
            out.append(r)
    return out


def adapt_claude_code(since: float, project: str | None, extra: list[str]):
    for root in claude_roots(extra):
        for f in root.glob("*/*.jsonl"):
            try:
                if f.stat().st_mtime < since:
                    continue
            except OSError:
                continue
            if project and project.lower() not in f.parent.name.lower():
                continue
            s = new_session(f"claude-code:{f.stem}", "claude-code")
            pending: dict[str, tuple[str, float | None]] = {}
            try:
                fh = f.open(encoding="utf-8", errors="replace")
            except OSError:
                continue
            with fh:
                for line in fh:
                    try:
                        r = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    typ = r.get("type")
                    ts = iso_to_epoch(r.get("timestamp"))
                    if typ == "summary" and r.get("summary"):
                        s["title"] = s["title"] or str(r["summary"])[:120]
                    if typ not in ("user", "assistant"):
                        continue
                    touch(s, ts)
                    if r.get("cwd") and not s["cwd"]:
                        s["cwd"] = r["cwd"]
                    if r.get("promptSource") == "sdk" or r.get("entrypoint") in ("sdk-cli", "sdk-ts", "sdk-py"):
                        s["automated"] = True
                    msg = r.get("message") or {}
                    content = msg.get("content")
                    if typ == "user" and not r.get("isSidechain"):
                        if isinstance(content, str):
                            add_turn(s, ts, content)
                        elif isinstance(content, list):
                            for x in content:
                                if x.get("type") == "text":
                                    add_turn(s, ts, x.get("text"))
                    if isinstance(content, list):
                        for x in content:
                            if x.get("type") == "tool_use":
                                fam = add_call(s, x.get("name"), x.get("input"), ts, x.get("id"))
                                if fam:
                                    pending[x.get("id")] = (fam, ts)
                            elif x.get("type") == "tool_result":
                                if x.get("tool_use_id") not in pending:
                                    continue
                                fam, t0 = pending.pop(x.get("tool_use_id"), (None, None))
                                c = x.get("content")
                                text = c if isinstance(c, str) else json.dumps(c)[:600] if c else ""
                                dur = (ts - t0) if (ts and t0) else None
                                add_result(s, fam, bool(x.get("is_error")), text, dur)
            yield s


def adapt_codex(since: float, project: str | None, extra: list[str]):
    roots = [Path.home() / ".codex" / "sessions"]
    if SCAN_WSL and Path("/mnt/c/Users").is_dir():
        roots += [Path(p) for p in glob.glob("/mnt/c/Users/*/.codex/sessions")]
    roots += [Path(e).expanduser() for e in extra]
    for root in roots:
        if not root.is_dir():
            continue
        for f in root.rglob("rollout-*.jsonl"):
            try:
                if f.stat().st_mtime < since:
                    continue
            except OSError:
                continue
            s = new_session(f"codex:{f.stem.replace('rollout-', '')}", "codex")
            pending = {}
            with f.open(encoding="utf-8", errors="replace") as fh:
                for line in fh:
                    try:
                        r = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    ts = iso_to_epoch(r.get("timestamp"))
                    touch(s, ts)
                    p = r.get("payload") or {}
                    pt = p.get("type")
                    if r.get("type") == "session_meta":
                        s["cwd"] = p.get("cwd", "")
                    elif r.get("type") == "event_msg" and pt == "user_message":
                        add_turn(s, ts, p.get("message"))
                    elif r.get("type") == "response_item" and pt in ("function_call", "local_shell_call", "custom_tool_call"):
                        args = p.get("arguments") or p.get("action") or p.get("input") or {}
                        if isinstance(args, str):
                            try:
                                args = json.loads(args)
                            except json.JSONDecodeError:
                                args = {"command": args}
                        fam = add_call(s, p.get("name") or pt, args if isinstance(args, dict) else {}, ts, p.get("call_id"))
                        if fam:
                            pending[p.get("call_id")] = (fam, ts)
                    elif r.get("type") == "response_item" and pt in ("function_call_output", "custom_tool_call_output"):
                        if p.get("call_id") not in pending:
                            continue
                        fam, t0 = pending.pop(p.get("call_id"), (None, None))
                        out = p.get("output")
                        text = out if isinstance(out, str) else json.dumps(out)[:600]
                        err = bool(re.search(r'"exit_code":\s*[1-9]|Process exited with code [1-9]', text[:800] or ""))
                        add_result(s, fam, err, text, (ts - t0) if ts and t0 else None)
            if project and project.lower() not in (s["cwd"] or "").lower():
                continue
            yield s


def hermes_dbs(extra: list[str]) -> list[Path]:
    homes = [Path(os.environ.get("HERMES_REAL_HOME", str(Path.home()))) / ".hermes"]
    out = []
    for h in homes:
        if (h / "state.db").is_file():
            out.append(h / "state.db")
        out += sorted(h.glob("profiles/*/state.db"))
    out += [Path(e).expanduser() for e in extra]
    return [p for p in out if p.is_file()]


HERMES_AUTOMATED = {"cron", "subagent", "tool", "kanban", "api_server"}


def _hermes_err(text: str) -> bool:
    t = (text or "")[:600]
    return bool(re.search(r'"status":\s*"error"|"exit_code":\s*[1-9]|"error":\s*"(?!")|"success":\s*false', t))


def adapt_hermes(since: float, project: str | None, extra: list[str]):
    for db in hermes_dbs(extra):
        label = "hermes" if db.parent.name == ".hermes" else f"hermes-{db.parent.name}"
        try:
            con = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=5)
        except sqlite3.Error:
            continue
        try:
            parent = dict(con.execute("select id, parent_session_id from sessions").fetchall())
        except sqlite3.Error:
            parent = {}

        def root_of(x, _p=parent):
            seen = set()
            while _p.get(x) and x not in seen:
                seen.add(x)
                x = _p[x]
            return x
        try:
            sess = con.execute(
                "select id, source, started_at, ended_at, coalesce(title,''), coalesce(cwd,'') "
                "from sessions where started_at >= ? order by started_at", (since,)).fetchall()
        except sqlite3.Error:
            continue
        for sid, src, st, en, title, cwd in sess:
            if project and project.lower() not in (cwd or "").lower() and project.lower() not in (title or "").lower():
                continue
            s = new_session(f"{label}:{sid}", label)
            s["lineage"] = f"{label}:{root_of(sid)}"
            s["title"], s["cwd"] = title[:120], cwd
            s["automated"] = src in HERMES_AUTOMATED
            s["channel"] = src
            touch(s, st)
            touch(s, en)
            pending = {}
            for role, content, tcalls, tcid, ts in con.execute(
                    "select role, case when role='tool' then substr(content,1,600) else content end, "
                    "tool_calls, tool_call_id, timestamp from messages where session_id=? order by id", (sid,)):
                touch(s, ts)
                if role == "user":
                    add_turn(s, ts, content)
                elif role == "assistant" and tcalls:
                    try:
                        calls = json.loads(tcalls)
                    except json.JSONDecodeError:
                        calls = []
                    for c in calls or []:
                        fn = (c.get("function") or {})
                        try:
                            args = json.loads(fn.get("arguments") or "{}")
                        except json.JSONDecodeError:
                            args = {}
                        cid = c.get("id") or c.get("call_id")
                        fam = add_call(s, fn.get("name"), args if isinstance(args, dict) else {}, ts, cid)
                        if fam:
                            pending[cid] = (fam, ts)
                elif role == "tool":
                    if tcid not in pending:
                        continue
                    fam, t0 = pending.pop(tcid, (None, None))
                    add_result(s, fam, _hermes_err(content or ""), content or "", (ts - t0) if ts and t0 else None)
            yield s
        con.close()


OBS_SKIP_DEFAULT = {"claude-code", "hermes-cog", "codex"}  # read raw instead (richer)


def adapt_observatory(since: float, project: str | None, extra: list[str], skip: set[str]):
    ws = find_cog_workspace()
    roots = ([ws / ".cog" / "state" / "conversations"] if ws else []) + [Path(e) for e in extra]
    for root in roots:
        meta_p = root / "_meta.json"
        if not meta_p.is_file():
            continue
        try:
            meta = json.loads(meta_p.read_text())
        except (OSError, json.JSONDecodeError):
            continue
        for sid, m in meta.items():
            src = m.get("source", "")
            if src in skip:
                continue
            last = iso_to_epoch(m.get("last_turn_at"))
            if last is None or last < since:
                continue
            f = root / (sid.replace("/", "__", 1) + ".json")
            if not f.is_file():
                continue
            try:
                turns = json.loads(f.read_text())
            except (OSError, json.JSONDecodeError):
                continue
            s = new_session(f"obs:{sid}", f"obs:{src}")
            s["title"] = (m.get("title") or "")[:120]
            for t in turns if isinstance(turns, list) else []:
                ts = iso_to_epoch(t.get("timestamp"))
                if ts is not None and ts < since:
                    continue
                touch(s, ts)
                if t.get("role") == "user":
                    add_turn(s, ts, t.get("text"))
            if project and project.lower() not in (s["title"] or "").lower():
                continue
            yield s


# ----------------------------------------------------------------------------- cross-session analysis

def cluster_turns(items: list[dict], threshold: float, min_sessions: int, cap: int) -> list[dict]:
    """Greedy clustering of operator utterances by content-word Jaccard; keep cross-session clusters."""
    clusters: list[dict] = []
    index: dict[str, list[int]] = collections.defaultdict(list)
    for it in items:
        words = set(content_words(it["text"]))
        if len(words) < 3:
            continue
        best, best_sim = None, 0.0
        cand = set()
        for w in words:
            cand.update(index.get(w, ())[-200:])
        for ci in cand:
            sim = jaccard(words, clusters[ci]["words"])
            if sim > best_sim:
                best, best_sim = ci, sim
        if best is not None and best_sim >= threshold:
            c = clusters[best]
            c["members"].append(it)
            c["words"] = c["words"] | words if len(c["words"]) < 40 else c["words"]
        else:
            clusters.append({"words": words, "members": [it]})
            best = len(clusters) - 1
        for w in words:
            index[w].append(best)
    out = []
    for c in clusters:
        sess = {m["session"] for m in c["members"]}
        lines = {m.get("lineage", m["session"]) for m in c["members"]}
        if len(lines) < min_sessions:
            continue
        common = collections.Counter(w for m in c["members"] for w in set(content_words(m["text"])))
        out.append({
            "key": " ".join(w for w, _ in common.most_common(5)),
            "sessions": len(lines), "occurrences": len(c["members"]),
            "first_seen": epoch_to_iso(min((m["ts"] or 0) for m in c["members"]) or None),
            "last_seen": epoch_to_iso(max((m["ts"] or 0) for m in c["members"]) or None),
            "quotes": [quote(m["text"], 200) for m in c["members"][:3]],
            "evidence": sorted(sess)[:6],
        })
    out.sort(key=lambda x: (x["sessions"], x["occurrences"]), reverse=True)
    return out[:cap]


def analyze(sessions: list[dict], args) -> dict:
    human = [s for s in sessions if s["operator_turns"] and not s["automated"]]
    automated = [s for s in sessions if s["automated"]]
    trivial = [s for s in sessions if not s["operator_turns"] and s["tool_calls"] == 0]

    # Dedup operator utterances that appear in several sources (same text same day).
    seen: dict = {}
    seen_lineage: set = set()
    utter, corrections = [], []
    for s in human:
        for t in s["operator_turns"]:
            bucket = int((t["ts"] or 0) // 180)  # mirrored copies of one utterance land within minutes
            key = (re.sub(r"\W+", " ", t["text"].lower()).strip()[:300], bucket)
            if key in seen:
                continue  # same words within minutes: a mirror or a compaction copy, not a repeat
            seen[key] = s["source"]
            lk = (s.get("lineage") or s["id"], re.sub(r"\W+", " ", t["text"].lower()).strip()[:300])
            if lk in seen_lineage:
                continue  # compaction copy of the same turn in a child session
            seen_lineage.add(lk)
            it = {"session": s["id"], "lineage": s.get("lineage") or s["id"], "ts": t["ts"], "text": t["text"]}
            if len(t["text"]) <= 1500:
                utter.append(it)
                if CORRECTION.search(t["text"][:600]):
                    corrections.append(it)

    repeated_instr = cluster_turns(utter, args.sim, 2, 25)
    repeated_corr = cluster_turns(corrections, max(0.3, args.sim - 0.1), 2, 25)

    # Commands
    shape_sessions = collections.defaultdict(set)
    shape_count = collections.Counter()
    long_cmds = collections.defaultdict(list)
    inc_meta = collections.defaultdict(list)
    for s in sessions:
        lin = s.get("lineage") or s["id"]
        for cmd in s["commands"]:
            sh = bash_shape(cmd)
            if not sh:
                continue
            shape_sessions[sh].add(lin)
            shape_count[sh] += 1
            nc = norm_command(cmd)
            if len(nc) >= 50:
                long_cmds[nc].append((lin, cmd))
                inc_meta[nc].append((s["end"] or s["start"] or 0, s["automated"]))
    repeated_cmds = [
        {"shape": sh, "calls": shape_count[sh], "sessions": len(shape_sessions[sh]),
         "evidence": sorted(shape_sessions[sh])[:4]}
        for sh in shape_count if len(shape_sessions[sh]) >= 3]
    repeated_cmds.sort(key=lambda x: (x["sessions"], x["calls"]), reverse=True)
    incantations = []
    for nc, lst in long_cmds.items():
        ss = {sid for sid, _ in lst}
        if len(ss) >= 3:
            meta = inc_meta[nc]
            incantations.append({"pattern": quote(nc, 200), "calls": len(lst), "sessions": len(ss),
                                 "last_seen": epoch_to_iso(max(t for t, _ in meta) or None),
                                 "automated_share": round(sum(1 for _, a_ in meta if a_) / max(1, len(meta)), 2),
                                 "example": quote(lst[-1][1], 240), "evidence": sorted(ss)[:4]})
    incantations.sort(key=lambda x: (x["sessions"], x["calls"]), reverse=True)

    # Errors / denials / slow
    fam_calls, fam_err, fam_err_sess = collections.Counter(), collections.Counter(), collections.defaultdict(set)
    fam_err_text = collections.defaultdict(collections.Counter)
    slow = collections.defaultdict(list)
    waits = collections.defaultdict(list)
    for s in sessions:
        fam_calls.update(s["families"])
        fam_err.update(s["family_errors"])
        for fam in s["family_errors"]:
            fam_err_sess[fam].add(s.get("lineage") or s["id"])
        for fam, txts in s["error_texts"].items():
            for x in txts:
                fam_err_text[fam][re.sub(r"\d+", "N", x)[:120]] += 1
        for fam, d in s["slow_calls"]:
            (waits if WAIT_FAMILIES.match(fam) else slow)[fam].append((d, s["id"]))
    error_hotspots = [
        {"family": f, "errors": fam_err[f], "calls": fam_calls[f], "rate": round(fam_err[f] / max(1, fam_calls[f]), 3),
         "sessions": len(fam_err_sess[f]), "evidence": sorted(fam_err_sess[f])[:4],
         "top_errors": [{"error": e, "n": n} for e, n in fam_err_text[f].most_common(4)]}
        for f in fam_err if fam_err[f] >= 5 and len(fam_err_sess[f]) >= 2]
    error_hotspots.sort(key=lambda x: x["errors"], reverse=True)
    sig_n, sig_sess = collections.Counter(), collections.defaultdict(set)
    sig_auto, sig_times = collections.defaultdict(set), collections.defaultdict(list)
    for s in sessions:
        for sig, n in s["error_sigs"].items():
            sig_n[sig] += n
            lin = s.get("lineage") or s["id"]
            sig_sess[sig].add(lin)
            if s["automated"]:
                sig_auto[sig].add(lin)
            if s["start"]:
                sig_times[sig].append(s["end"] or s["start"])
    error_signatures = [{"signature": g, "errors": sig_n[g], "sessions": len(sig_sess[g]),
                         "automated_share": round(len(sig_auto[g]) / max(1, len(sig_sess[g])), 2),
                         "first_seen": epoch_to_iso(min(sig_times[g])) if sig_times[g] else None,
                         "last_seen": epoch_to_iso(max(sig_times[g])) if sig_times[g] else None,
                         "evidence": sorted(sig_sess[g])[:4]}
                        for g in sig_n if len(sig_sess[g]) >= 3]
    error_signatures.sort(key=lambda x: (x["sessions"], x["errors"]), reverse=True)
    time_sinks = [
        {"family": f, "slow_calls": len(v), "total_s": round(sum(d for d, _ in v)), "max_s": max(d for d, _ in v),
         "sessions": len({sid for _, sid in v}), "evidence": sorted({sid for _, sid in v})[:4]}
        for f, v in slow.items() if len(v) >= 3]
    time_sinks.sort(key=lambda x: x["total_s"], reverse=True)
    wait_summary = {f: {"calls": len(v), "total_s": round(sum(d for d, _ in v)), "sessions": len({i for _, i in v})}
                    for f, v in waits.items()}

    skills = collections.Counter()
    for s in sessions:
        skills.update(s["skills"])

    by_source = collections.Counter(s["source"] for s in sessions)
    by_source_human = collections.Counter(s["source"] for s in human)
    corr_rate = round(len(corrections) / max(1, len(utter)), 3)

    return {
        "totals": {
            "sessions": len(sessions), "operator_sessions": len(human), "automated_sessions": len(automated),
            "trivial_sessions": len(trivial), "operator_utterances": len(utter), "correction_utterances": len(corrections),
            "correction_rate": corr_rate, "tool_calls": sum(fam_calls.values()), "tool_errors": sum(fam_err.values()),
            "denied_calls": sum(s["denied"] for s in sessions),
        },
        "by_source": dict(by_source.most_common()),
        "by_source_operator": dict(by_source_human.most_common()),
        "top_tool_families": dict(fam_calls.most_common(20)),
        "repeated_instructions": repeated_instr,
        "repeated_corrections": repeated_corr,
        "repeated_commands": repeated_cmds[:20],
        "incantations": incantations[:12],
        "error_hotspots": error_hotspots[:15],
        "error_signatures": error_signatures[:25],
        "time_sinks": time_sinks[:12],
        "waits": wait_summary,
        "correction_sample": [
            {"session": c["session"], "at": epoch_to_iso(c["ts"]), "text": quote(c["text"], 180)}
            for c in sorted(corrections, key=lambda x: -(x["ts"] or 0))[:args.sample]],
        "denials": sorted(({"session": s["id"], "denied": s["denied"]} for s in sessions if s["denied"]),
                          key=lambda x: -x["denied"])[:15],
        "skill_invocations": dict(skills.most_common(60)),
    }


def session_line(s: dict) -> dict:
    first = s["operator_turns"][0]["text"] if s["operator_turns"] else ""
    return {
        "id": s["id"], "source": s["source"], "channel": s.get("channel", ""), "automated": s["automated"],
        "start": epoch_to_iso(s["start"]), "end": epoch_to_iso(s["end"]), "title": redact(s["title"]),
        "cwd": s["cwd"], "operator_turns": len(s["operator_turns"]), "first_prompt": quote(first, 240),
        "tool_calls": s["tool_calls"], "tool_errors": s["tool_errors"], "denied": s["denied"],
        "top_families": dict(s["families"].most_common(6)), "skills": dict(s["skills"]),
        "corrections": [quote(t["text"], 200) for t in s["operator_turns"] if CORRECTION.search(t["text"][:600])][:5],
    }


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--since", default="30d", help="window: 7d, 12h, 2w or ISO date (default 30d)")
    ap.add_argument("--sources", default="auto",
                    help="auto | comma list of claude-code,codex,hermes,observatory (default auto = all found)")
    ap.add_argument("--project", help="substring filter on project dir / cwd / title")
    ap.add_argument("--run", help="run id (default: timestamp); reuses the dir if it exists")
    ap.add_argument("--claude-root", action="append", default=[], help="extra ~/.claude/projects root")
    ap.add_argument("--codex-root", action="append", default=[], help="extra ~/.codex/sessions root")
    ap.add_argument("--hermes-db", action="append", default=[], help="extra Hermes state.db path")
    ap.add_argument("--observatory-root", action="append", default=[], help="extra observatory conversations dir")
    ap.add_argument("--observatory-skip", default=",".join(sorted(OBS_SKIP_DEFAULT)),
                    help="observatory sources to skip because they are read raw")
    ap.add_argument("--sim", type=float, default=0.45, help="Jaccard threshold for clustering utterances")
    ap.add_argument("--include-automated", action="store_true",
                    help="count operator text in automated sessions (cron/sdk/subagent) too")
    ap.add_argument("--sample", type=int, default=60, help="recent correction utterances to include verbatim")
    ap.add_argument("--operator", action="append", default=[],
                    help="speaker tag(s) that are the operator in multi-user chats, e.g. 'Slow Bro'")
    a = ap.parse_args(argv)
    OPERATORS[:] = a.operator

    since = parse_since(a.since)
    want = {"claude-code", "codex", "hermes", "observatory"} if a.sources == "auto" else set(a.sources.split(","))
    t0 = time.time()
    sessions = []
    errors = []
    adapters = [
        ("claude-code", lambda: adapt_claude_code(since, a.project, a.claude_root)),
        ("codex", lambda: adapt_codex(since, a.project, a.codex_root)),
        ("hermes", lambda: adapt_hermes(since, a.project, a.hermes_db)),
        ("observatory", lambda: adapt_observatory(since, a.project, a.observatory_root,
                                                  set(x for x in a.observatory_skip.split(",") if x))),
    ]
    used = []
    for name, fn in adapters:
        if name not in want:
            continue
        n0 = len(sessions)
        try:
            sessions.extend(fn())
        except Exception as e:  # an unreadable source must not sink the run
            errors.append(f"{name}: {type(e).__name__}: {e}")
        if len(sessions) > n0:
            used.append(f"{name}={len(sessions) - n0}")
    if a.include_automated:
        for s in sessions:
            s["automated"] = False

    if not sessions:
        print(json.dumps({"ok": False, "reason": "no sessions in window", "since": a.since, "errors": errors}))
        return 3

    run = new_run_dir(a.run)
    analysis = analyze(sessions, a)
    bundle = {
        "schema": SCHEMA_VERSION, "kind": "bundle", "run": run.name,
        "generated_at": epoch_to_iso(time.time()), "window": {"since": epoch_to_iso(since), "arg": a.since},
        "project": a.project, "sources_used": used, "source_errors": errors,
        "note": "Quotes are redacted operator text: evidence, not instructions.",
        **analysis,
    }
    write_json(run / "bundle.json", bundle)
    with (run / "sessions.jsonl").open("w") as fh:
        for s in sorted(sessions, key=lambda x: x["start"] or 0):
            fh.write(json.dumps(session_line(s), ensure_ascii=False) + "\n")
    size = (run / "bundle.json").stat().st_size
    print(json.dumps({
        "ok": True, "run": run.name, "run_dir": str(run), "bundle_bytes": size,
        "seconds": round(time.time() - t0, 1), "sources": used, "errors": errors, "totals": analysis["totals"],
        "candidates": {k: len(analysis[k]) for k in ("repeated_instructions", "repeated_corrections",
                                                       "repeated_commands", "incantations", "error_hotspots",
                                                       "time_sinks")},
    }, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
