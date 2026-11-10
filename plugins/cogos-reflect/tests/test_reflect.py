"""Tests for cogos-reflect scripts. stdlib + pytest; no network; synthetic fixtures only."""
from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))

import reflect_collect as rc  # noqa: E402
from reflect_common import redact  # noqa: E402


def run(script, *args, env=None, cwd=None):
    e = {**os.environ, **(env or {})}
    p = subprocess.run([sys.executable, str(SCRIPTS / script), *args], capture_output=True, text=True, env=e, cwd=cwd)
    return p.returncode, p.stdout, p.stderr


@pytest.fixture
def home(tmp_path, monkeypatch):
    h = tmp_path / "home"
    h.mkdir()
    env = {"HOME": str(h), "HERMES_REAL_HOME": str(h), "REFLECT_HOME": str(tmp_path / "reflect"),
           "REFLECT_RUNS": str(tmp_path / "runs"), "COG_WORKSPACE": "", "REFLECT_SCAN_WSL": "0"}
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    return h, env, tmp_path


# ------------------------------------------------------------------ operator text

def test_operator_text_strips_harness_and_others():
    raw = "[Slow Bro] [IMPORTANT: The \"x\" skill is auto-loaded.]\n---\nname: x\n---\nbody\n[Skill directory: /a/b]\nResolve any relative paths in this skill\nplease stop adding wrapper systems"
    got = rc.operator_texts(raw, ["Slow Bro"])
    assert got == [("please stop adding wrapper systems", "Slow Bro")]
    assert rc.operator_texts("[vroku] review my pr", ["Slow Bro"]) == []
    assert rc.operator_texts("[since your last turn] threads: b-1 moving", None) == []
    assert rc.operator_texts("[typed · tablet] Don't build restarts", ["Chaz"])[0][0] == "Don't build restarts"
    two = rc.operator_texts("[Slow Bro] one thing [New message] [Slow Bro] another thing", ["Slow Bro"])
    assert [t for t, _ in two] == ["one thing", "another thing"]
    assert rc.operator_texts("https://example.com/x [screenshot]", None) == []


def test_brief_detection():
    assert rc.is_brief("You are reviewing a change you did not write. Repo: x")
    assert rc.is_brief("[stage work thread] You are Cog's work thread")
    assert not rc.is_brief("can you look at the stage server")


def test_bash_shape_and_norm():
    assert rc.bash_shape("cd /x && git status --short | head") == "git status --short"
    assert rc.bash_shape("FOO=1 timeout 30 python3 -c 'print(1)'") == "python3 -c"
    assert rc.norm_command("gh pr view 123 -R a/b --json state -q '.x'") == "gh pr view N -R a/b --json state -q S"


def test_replayed_tool_calls_count_once():
    rc.SEEN_CALLS.clear()
    s1, s2 = rc.new_session("a", "x"), rc.new_session("b", "x")
    assert rc.add_call(s1, "Bash", {"command": "ls"}, None, "id-1")
    assert rc.add_call(s2, "Bash", {"command": "ls"}, None, "id-1") is None
    assert (s1["tool_calls"], s2["tool_calls"]) == (1, 0)


def test_redaction():
    s = "key sk-ant-abcdefghijklmnopqrstuvwxyz and ghp_abcdefghijklmnopqrstuvwxyz0123 /mcp-connector/abcdefghijkl/mcp"
    r = redact(s)
    assert "sk-ant" not in r and "ghp_" not in r and "abcdefghijkl/mcp" not in r


# ------------------------------------------------------------------ collect end to end

def _claude_session(root: Path, sid: str, prompts: list[str], cmds: list[str], err: bool = False, day: int = 1,
                    err_text: str = "x"):
    p = root / ".claude" / "projects" / "-repo"
    p.mkdir(parents=True, exist_ok=True)
    lines = []
    t = 0
    for i, pr in enumerate(prompts):
        lines.append({"type": "user", "timestamp": f"2099-01-{day:02d}T00:0{t}:00Z", "sessionId": sid, "cwd": "/repo",
                      "message": {"role": "user", "content": pr}})
        t += 1
    for i, c in enumerate(cmds):
        lines.append({"type": "assistant", "timestamp": f"2099-01-01T00:1{i}:00Z",
                      "message": {"content": [{"type": "tool_use", "id": f"{sid}-t{i}", "name": "Bash", "input": {"command": c}}]}})
        lines.append({"type": "user", "timestamp": f"2099-01-01T00:1{i}:30Z",
                      "message": {"content": [{"type": "tool_result", "tool_use_id": f"{sid}-t{i}", "is_error": err,
                                               "content": err_text}]}})
    (p / f"{sid}.jsonl").write_text("\n".join(json.dumps(x) for x in lines))


def _hermes_db(root: Path):
    d = root / ".hermes" / "profiles" / "p1"
    d.mkdir(parents=True)
    con = sqlite3.connect(d / "state.db")
    con.execute("create table sessions(id text, source text, started_at real, ended_at real, title text, cwd text)")
    con.execute("create table messages(id integer primary key, session_id text, role text, content text, "
                "tool_calls text, tool_call_id text, timestamp real)")
    now = 4070908800  # 2099
    con.execute("insert into sessions values('h1','discord',?,?,'t','/repo')", (now, now + 100))
    con.execute("insert into messages(session_id,role,content,timestamp) values('h1','user',?,?)",
                ("[Slow Bro] don't use openrouter for claude models again", now))
    con.execute("insert into messages(session_id,role,content,tool_calls,timestamp) values('h1','assistant','',?,?)",
                (json.dumps([{"id": "c1", "function": {"name": "terminal", "arguments": json.dumps({"command": "ls -la"})}}]), now + 1))
    con.execute("insert into messages(session_id,role,content,tool_call_id,timestamp) values('h1','tool',?,?,?)",
                ('{"exit_code": 1, "error": "boom"}', "c1", now + 2))
    con.execute("insert into sessions values('h2','cron',?,?,'c','/repo')", (now, now + 5))
    con.execute("insert into messages(session_id,role,content,timestamp) values('h2','user','[IMPORTANT: You are running as a scheduled cron job.]',?)", (now,))
    con.commit()
    con.close()


def test_collect_end_to_end(home):
    h, env, tmp = home
    for i in range(3):
        _claude_session(h, f"s{i}", ["don't use openrouter for claude models, use the oauth lane",
                                     "run the tests before pushing"],
                        ["gh pr view 12 -R org/repo --json state,title,reviewDecision -q '.state'", "ls"], day=i + 1)
    _hermes_db(h)
    code, out, err = run("reflect_collect.py", "--since", "2000-01-01", "--run", "t1", "--operator", "Slow Bro", env=env)
    assert code == 0, err
    s = json.loads(out)
    assert s["ok"] and s["totals"]["operator_sessions"] == 4 and s["totals"]["automated_sessions"] == 1
    b = json.loads((tmp / "runs" / "t1" / "bundle.json").read_text())
    keys = [c["key"] for c in b["repeated_corrections"]]
    assert any("openrouter" in k for k in keys)
    assert b["repeated_corrections"][0]["sessions"] >= 3
    assert any(i["sessions"] == 3 for i in b["incantations"])
    assert (tmp / "runs" / "t1" / "sessions.jsonl").read_text().count("\n") == 5


def test_collect_no_sessions(home):
    h, env, tmp = home
    code, out, _ = run("reflect_collect.py", "--since", "1d", env=env)
    assert code == 3 and json.loads(out)["ok"] is False


SECRET = "ghp_abcdefghijklmnopqrstuvwxyz0123456789"


def test_collect_redacts_every_output(home):
    """A secret in a repeated prompt, a command and an error must not reach any artifact."""
    h, env, tmp = home
    for i in range(3):
        _claude_session(h, f"r{i}", [f"never push with {SECRET} again, use the deploy key",
                                     "always run the linter first please"],
                        [f"token={SECRET} git push origin main",
                         f"curl -sS -H 'Authorization: Bearer {SECRET}' https://api.example.com/v1/repos/org/name/deployments",
                         "mysql -pHunter2Secret -uroot production_db --batch --execute status"],
                        err=True, day=i + 1, err_text=f'{{"error": "auth failed for api_key={SECRET} at /x"}}')
    code, out, err = run("reflect_collect.py", "--since", "2000-01-01", "--run", "sec", env=env)
    assert code == 0, err
    rd = tmp / "runs" / "sec"
    for f in ("bundle.json", "sessions.jsonl"):
        text = (rd / f).read_text().lower()
        assert SECRET.lower() not in text, f"secret leaked into {f}"
        assert "hunter2secret" not in text, f"short-flag secret leaked into {f}"
        assert str(h).lower() not in text, f"home path leaked into {f}"
    b = json.loads((rd / "bundle.json").read_text())
    assert b["repeated_corrections"], "the repeated correction should still cluster"
    assert b["incantations"] and b["error_signatures"], "error and incantation paths must be exercised"
    assert all(SECRET.lower() not in c["key"] for c in b["repeated_corrections"] + b["repeated_instructions"])


def test_collect_dedupes_replayed_calls_per_adapter(home):
    """Same tool_use id in two Claude Code files (a resumed session) counts once."""
    h, env, tmp = home
    rc.SEEN_CALLS.clear()
    _claude_session(h, "a", ["first prompt here for the session"], ["ls -la"], day=1)
    p = h / ".claude" / "projects" / "-repo"
    (p / "b.jsonl").write_text((p / "a.jsonl").read_text().replace('"sessionId": "a"', '"sessionId": "b"'))
    code, out, err = run("reflect_collect.py", "--since", "2000-01-01", "--run", "dd", env=env)
    assert code == 0, err
    assert json.loads(out)["totals"]["tool_calls"] == 1


def test_collect_dedupes_codex_and_hermes(home):
    h, env, tmp = home
    cx = h / ".codex" / "sessions" / "2099"
    cx.mkdir(parents=True)
    ev = [{"timestamp": "2099-01-01T00:00:00Z", "type": "session_meta", "payload": {"cwd": "/repo"}},
          {"timestamp": "2099-01-01T00:00:01Z", "type": "event_msg", "payload": {"type": "user_message", "message": "run it"}},
          {"timestamp": "2099-01-01T00:00:02Z", "type": "response_item",
           "payload": {"type": "function_call", "name": "shell", "call_id": "cx-1", "arguments": "{\"command\": \"ls\"}"}}]
    for n in ("rollout-a", "rollout-b"):  # the same call replayed in a second rollout
        (cx / f"{n}.jsonl").write_text("\n".join(json.dumps(e) for e in ev))
    _hermes_db(h)
    con = sqlite3.connect(h / ".hermes" / "profiles" / "p1" / "state.db")
    now = 4070908800
    con.execute("insert into sessions values('h3','discord',?,?,'t','/repo')", (now, now + 9))
    con.execute("insert into messages(session_id,role,content,tool_calls,timestamp) values('h3','assistant','',?,?)",
                (json.dumps([{"id": "c1", "function": {"name": "terminal", "arguments": "{}"}}]), now + 1))
    con.commit()
    con.close()
    code, out, err = run("reflect_collect.py", "--since", "2000-01-01", "--sources", "codex,hermes", "--run", "dd2", env=env)
    assert code == 0, err
    assert json.loads(out)["totals"]["tool_calls"] == 2  # cx-1 once, hermes c1 once


def test_collect_main_is_reentrant(home):
    h, env, tmp = home
    _claude_session(h, "a", ["first prompt here for the session"], ["ls -la"], day=1)
    env2 = {**os.environ, **env}
    old = dict(os.environ)
    os.environ.update(env2)
    try:
        import io, contextlib
        for rid in ("re1", "re2"):
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                assert rc.main(["--since", "2000-01-01", "--run", rid]) == 0
            assert json.loads(buf.getvalue())["totals"]["tool_calls"] == 1
    finally:
        os.environ.clear()
        os.environ.update(old)


def test_bash_shape_drops_assignments():
    assert "hunter2" not in rc.bash_shape("token=hunter2secret git push")
    assert rc.bash_shape("token=hunter2secret git push") == "git push"
    assert rc.bash_shape("mysql -pHunter2Secret -uroot db") == "mysql -p -u"


def test_no_quadratic_backtracking():
    import time
    t = time.time()
    rc.operator_texts("[NOTE x " * 20000, None)
    rc.operator_texts("[Skill directory: " + "y" * 200000, None)
    assert time.time() - t < 0.5


def test_run_id_cannot_escape(home):
    _, env, tmp = home
    code, _, err = run("reflect_collect.py", "--since", "2000-01-01", "--run", "../escape", env=env)
    assert code == 2 and not (tmp / "escape").exists()


def test_skills_never_writes_to_cwd(home, tmp_path):
    h, env, tmp = home
    work = tmp_path / "cwd"
    work.mkdir()
    code, out, _ = run("reflect_skills.py", env=env, cwd=work)
    assert code == 0 and not (work / "skills.json").exists()
    code, _, _ = run("reflect_skills.py", "--out", str(work / "x.json"), env=env, cwd=work)
    assert code == 2 and not (work / "x.json").exists()


def test_recur_output_is_redacted(home, tmp_path):
    h, env, _ = home
    mem = h / ".claude" / "projects" / "p" / "memory"
    mem.mkdir(parents=True)
    (mem / "m.md").write_text(f"deploy key rotation: api_key={SECRET} for the staging deploy\n")
    out = run("reflect_recur.py", "deploy", "key", "staging", env=env, cwd=tmp_path)[1]
    assert json.loads(out)["hits"] and SECRET not in out


# ------------------------------------------------------------------ ledger

def test_ledger_fold_and_denial_memory(home):
    _, env, _ = home
    assert run("reflect_ledger.py", "add", "--id", "F-1", "--kind", "repeated-correction",
               "--title", "routes claude through openrouter", "--sessions", "3", env=env)[0] == 0
    assert run("reflect_ledger.py", "status", "--id", "F-1", "--to", "denied", "--reason", "not now", env=env)[0] == 0
    assert run("reflect_ledger.py", "status", "--id", "F-1", "--to", "denied", "--reason", "still not", env=env)[0] == 0
    code, out, _ = run("reflect_ledger.py", "check", "--title", "claude routed via openrouter again", env=env)
    v = json.loads(out)
    assert v["verdict"] == "denied-before" and v["previously_denied"][0]["id"] == "F-1"
    assert run("reflect_ledger.py", "status", "--id", "F-nope", "--to", "applied", env=env)[0] == 1
    st = json.loads(run("reflect_ledger.py", "stats", env=env)[1])
    assert st["by_status"] == {"denied": 1}
    # a malformed / hand-edited row must not break every reader
    lp = Path(run("reflect_ledger.py", "path", env=env)[1].strip())
    with lp.open("a") as fh:
        fh.write('{"id": "F-x"}\n{"id": "F-1", "op": "status"}\nnot json\n')
    code, out, _ = run("reflect_ledger.py", "stats", env=env)
    assert code == 0 and json.loads(out)["by_status"] == {"denied": 1}
    # bad types in a hand-edited row are tolerated too
    with lp.open("a") as fh:
        fh.write('{"id": "F-1", "op": "add", "kind": "other", "title": "t", "count": "lots"}\n')
    assert run("reflect_ledger.py", "stats", env=env)[0] == 0
    # everything persisted is redacted (the ledger is committed on CogOS)
    run("reflect_ledger.py", "status", "--id", "F-1", "--to", "denied", "--reason", f"no, that uses {SECRET}", env=env)
    run("reflect_ledger.py", "add", "--id", "F-2", "--kind", "other", "--title", f"key {SECRET} leaked",
        "--evidence", f"{env['HOME']}/x", env=env)
    assert SECRET not in lp.read_text() and env["HOME"] not in lp.read_text()
    # append-only: every write grows the file, nothing is rewritten
    before = lp.read_text()
    run("reflect_ledger.py", "status", "--id", "F-1", "--to", "deferred", env=env)
    assert lp.read_text().startswith(before)


# ------------------------------------------------------------------ recur / render / skills / backfill

def test_recur_finds_existing_lesson(home, tmp_path):
    h, env, _ = home
    sk = h / ".claude" / "skills" / "routing"
    sk.mkdir(parents=True)
    (sk / "SKILL.md").write_text("---\nname: routing\ndescription: x\n---\nNever route claude through openrouter; use oauth.\n")
    out = json.loads(run("reflect_recur.py", "claude", "openrouter", "oauth", env=env, cwd=tmp_path)[1])
    assert out["hits"] and out["hits"][0]["kind"] == "skill"


def test_render_check_and_cards(home):
    _, env, tmp = home
    rd = tmp / "runs" / "r1"
    (rd / "proposals" / "keep-claude-on-oauth").mkdir(parents=True)
    (rd / "findings.json").write_text(json.dumps({"run": "r1", "headline": "h", "findings": [
        {"id": "F-1", "kind": "repeated-correction", "title": "t", "sessions": 3, "occurrences": 4,
         "evidence": ["s1"], "impact": "i", "suggested_mechanism": "hook"}]}))
    (rd / "proposals" / "keep-claude-on-oauth" / "PROPOSAL.md").write_text("# Keep claude on oauth\n**What changes:** a\n")
    code, out, _ = run("reflect_render.py", "check", "--run", "r1", env=env)
    assert code == 1 and "missing" in out
    (rd / "proposals" / "keep-claude-on-oauth" / "PROPOSAL.md").write_text(
        "# Keep claude on oauth\n**What changes:** a\n**What you get:** b\n**Why you might say no:** c\n"
        "**Mechanism:** hook\n**Evidence:** 3 sessions\n**Exact change:** change.diff\n")
    (rd / "proposals" / "keep-claude-on-oauth" / "EVIDENCE.md").write_text("e")
    assert run("reflect_render.py", "check", "--run", "r1", env=env)[0] == 0
    code, out, _ = run("reflect_render.py", "cards", "--run", "r1", env=env)
    assert "Proposal 1 of 1" in out and (rd / "cards.md").exists()
    code, out, _ = run("reflect_render.py", "findings", "--run", "r1", "--html", env=env)
    assert code == 0 and (rd / "report.html").exists()


def test_skills_inventory_flags_broken_refs(home):
    h, env, tmp = home
    sk = h / ".claude" / "skills" / "old"
    sk.mkdir(parents=True)
    (sk / "SKILL.md").write_text(f"---\nname: old\ndescription: Use when x.\n---\nrun {h}/gone/tool.sh\n"
                                 "and /home/somebody-else/x.sh is another node's path, not broken here\n")
    (tmp / "runs" / "r1").mkdir(parents=True)
    code, out, _ = run("reflect_skills.py", env=env)
    assert code == 0 and json.loads(out)["with_broken_refs"] == 1


def test_backfill_idempotent(home, tmp_path):
    _, env, _ = home
    root = tmp_path / "lessons"
    root.mkdir()
    (root / "r.md").write_text("# Retro\n## Lessons\n- Always read the running config before editing the file on disk\n"
                               "## My own method failures\n### 1. Claimed the manifest needed no changes; it needed two\n"
                               "body text\n## Unrelated\n- not a lesson line, should be skipped entirely here\n")
    a = json.loads(run("reflect_backfill.py", "--root", str(root), env=env)[1])
    b = json.loads(run("reflect_backfill.py", "--root", str(root), env=env)[1])
    assert a["added"] == 2 and b["added"] == 0 and b["already"] == 2


# ------------------------------------------------------------------ skill files

SKILLS = Path(__file__).resolve().parents[1] / "skills"


@pytest.mark.parametrize("d", sorted(p.name for p in SKILLS.iterdir()))
def test_skill_frontmatter(d):
    t = (SKILLS / d / "SKILL.md").read_text()
    assert t.startswith("---\n")
    fm = t.split("\n---\n", 1)[0]
    assert f"name: {d}" in fm
    desc = [l for l in fm.splitlines() if l.startswith("description:")][0].split(":", 1)[1].strip().strip('"')
    assert desc.startswith("Use ") and len(desc) <= 70, desc
    assert len(t.splitlines()) <= 130
    assert "/home/" not in t and "/Users/" not in t
