#!/usr/bin/env python3
"""Spec tests for history.py — run as `python3 test_history.py` (no pytest).

Builds a synthetic ~/.claude fixture tree in a temp dir (via the `claude_dir`
seam) covering every parsing rule the specs name, then asserts the projects /
sessions / messages actions return exactly what history-data.md / browsing.md /
transcript.md require. No network, no dependencies beyond the stdlib.
"""
import json
import os
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import history  # noqa: E402

PASS = 0
FAIL = 0


def check(cond, msg):
    global PASS, FAIL
    if cond:
        PASS += 1
    else:
        FAIL += 1
        print(f"  FAIL: {msg}")


def eq(got, want, msg):
    check(got == want, f"{msg}\n        got:  {got!r}\n        want: {want!r}")


def jl(*objs):
    """Render objects as JSONL text, one per line. Raw strings pass through
    verbatim (so we can inject malformed / blank lines)."""
    out = []
    for o in objs:
        out.append(o if isinstance(o, str) else json.dumps(o))
    return "\n".join(out) + "\n"


def build_fixture(root):
    projects = os.path.join(root, "projects")
    os.makedirs(projects)

    # ---- Project A: -Users-jane-repo-a --------------------------------
    pa = os.path.join(projects, "-Users-jane-repo-a")
    os.makedirs(pa)

    # sess1: rename (customTitle) + summary + tool_use/tool_result + thinking +
    # usage tokens + isMeta + sidechain + malformed + skipped types.
    sess1 = jl(
        {"type": "summary", "summary": "A summary of the conversation"},
        {"type": "custom-title", "customTitle": "Custom Titled Session"},
        {"type": "user", "uuid": "u1", "sessionId": "s1",
         "timestamp": "2024-01-01T10:00:00Z", "cwd": "/Users/jane/repo-a",
         "message": {"role": "user", "content": "Hello there"}},
        {"type": "assistant", "uuid": "a1", "sessionId": "s1",
         "timestamp": "2024-01-01T10:01:00Z",
         "message": {"role": "assistant", "model": "claude-opus-4-1", "content": [
             {"type": "thinking", "thinking": "Let me think"},
             {"type": "text", "text": "Here is **markdown**"},
             {"type": "tool_use", "id": "tool1", "name": "Bash",
              "input": {"command": "ls"}}],
             "usage": {"input_tokens": 100, "output_tokens": 50,
                       "cache_creation_input_tokens": 10,
                       "cache_read_input_tokens": 5}}},
        {"type": "user", "uuid": "u2", "sessionId": "s1",
         "timestamp": "2024-01-01T10:02:00Z",
         "message": {"role": "user", "content": [
             {"type": "tool_result", "tool_use_id": "tool1",
              "content": "file1\nfile2", "is_error": False}]},
         "toolUseResult": {"stdout": "file1\nfile2"}},
        {"type": "user", "isMeta": True, "uuid": "m1", "sessionId": "s1",
         "timestamp": "2024-01-01T10:03:00Z",
         "message": {"role": "user", "content": "meta noise"}},
        {"type": "assistant", "uuid": "sc1", "isSidechain": True, "sessionId": "s1",
         "timestamp": "2024-01-01T10:04:00Z",
         "message": {"role": "assistant", "model": "claude-haiku",
                     "content": [{"type": "text", "text": "subagent reply"}],
                     "usage": {"input_tokens": 20, "output_tokens": 10}}},
        "{not valid json",
        {"type": "progress", "uuid": "p1"},
        {"type": "file-history-snapshot"},
        "",
    )
    open(os.path.join(pa, "sess1.jsonl"), "w").write(sess1)

    # sess2: local_command rename via <local-command-stdout> content.
    sess2 = jl(
        {"type": "user", "uuid": "u1", "sessionId": "s2",
         "timestamp": "2024-01-02T10:00:00Z",
         "message": {"role": "user", "content": "first message"}},
        {"type": "system", "subtype": "local_command", "uuid": "sys1",
         "sessionId": "s2", "timestamp": "2024-01-02T10:01:00Z",
         "content": "<local-command-stdout>Session renamed to: My Renamed Session"
                    "</local-command-stdout>"},
    )
    open(os.path.join(pa, "sess2.jsonl"), "w").write(sess2)

    # sess3: no rename/summary; first user text is a slash-command display (must
    # be skipped as a title source) then a genuine user question.
    sess3 = jl(
        {"type": "user", "uuid": "u1", "sessionId": "s3",
         "timestamp": "2024-01-03T10:00:00Z",
         "message": {"role": "user", "content": "<command-name>/clear</command-name>"}},
        {"type": "user", "uuid": "u2", "sessionId": "s3",
         "timestamp": "2024-01-03T10:01:00Z",
         "message": {"role": "user", "content": "What is the weather today?"}},
        {"type": "assistant", "uuid": "a1", "sessionId": "s3",
         "timestamp": "2024-01-03T10:02:00Z",
         "message": {"role": "assistant", "model": "claude-sonnet",
                     "content": [{"type": "text", "text": "It is sunny"}],
                     "usage": {"input_tokens": 10, "output_tokens": 5}}},
    )
    open(os.path.join(pa, "sess3.jsonl"), "w").write(sess3)

    # sess_empty: only skipped lines -> 0 messages -> dropped from listing.
    sess_empty = jl(
        {"type": "progress", "uuid": "p"},
        {"type": "file-history-snapshot"},
        {"type": "user", "isMeta": True, "uuid": "m", "sessionId": "s4",
         "timestamp": "2024-01-01T00:00:00Z",
         "message": {"role": "user", "content": "x"}},
        {"type": "user", "uuid": "nokeys",
         "message": {"role": "user", "content": "no session id or timestamp"}},
    )
    open(os.path.join(pa, "sess_empty.jsonl"), "w").write(sess_empty)

    # ---- Project B: -Users-jane-repo-b -------------------------------
    pb = os.path.join(projects, "-Users-jane-repo-b")
    os.makedirs(pb)
    long_text = "X" * 10005
    mainb = jl(
        {"type": "user", "uuid": "u1", "sessionId": "sb",
         "timestamp": "2024-01-05T10:00:00Z", "cwd": "/Users/jane/repo-b",
         "message": {"role": "user", "content": "Question B"}},
        {"type": "assistant", "uuid": "ab", "sessionId": "sb",
         "timestamp": "2024-01-05T10:01:00Z",
         "message": {"role": "assistant", "model": "claude-opus", "content": [
             {"type": "text", "text": long_text},
             {"type": "redacted_thinking"},
             {"type": "image", "source": {"type": "base64", "data": "aaa"}},
             {"type": "text", "text": "answer"}],
             "usage": {"input_tokens": 5, "output_tokens": 3}}},
    )
    open(os.path.join(pb, "main.jsonl"), "w").write(mainb)
    # A sidechain file inside a subdirectory: must NEVER be listed as a session.
    sub = os.path.join(pb, "subagent")
    os.makedirs(sub)
    open(os.path.join(sub, "side.jsonl"), "w").write(jl(
        {"type": "user", "uuid": "x", "sessionId": "z",
         "timestamp": "2024-01-05T09:00:00Z",
         "message": {"role": "user", "content": "sub"}}))

    # ---- Project C: no .jsonl -> not a project -----------------------
    pc = os.path.join(projects, "-Users-jane-empty-proj")
    os.makedirs(pc)
    open(os.path.join(pc, "notes.txt"), "w").write("not a session")

    # Deterministic mtimes: A newest (sess1 the freshest so cwd probe hits it),
    # B older, so projects sort A before B.
    now = time.time()
    os.utime(os.path.join(pa, "sess1.jsonl"), (now, now))
    os.utime(os.path.join(pa, "sess2.jsonl"), (now - 10, now - 10))
    os.utime(os.path.join(pa, "sess3.jsonl"), (now - 20, now - 20))
    os.utime(os.path.join(pa, "sess_empty.jsonl"), (now - 30, now - 30))
    os.utime(os.path.join(pb, "main.jsonl"), (now - 1000, now - 1000))


def test_projects(cdir):
    r = history.main(action="projects", claude_dir=cdir)
    projs = r.get("projects")
    check(isinstance(projs, list), "projects returns a list")
    slugs = [p["slug"] for p in projs]
    eq(slugs, ["-Users-jane-repo-a", "-Users-jane-repo-b"],
       "projects sorted by last_modified desc; empty-proj (no jsonl) excluded")
    a = projs[0]
    eq(a["name"], "repo-a", "project name = leaf of cwd-probed path")
    eq(a["path"], "/Users/jane/repo-a", "path from first cwd in newest session")
    eq(a["session_count"], 4, "session_count counts all top-level .jsonl (incl empty)")
    check(isinstance(a["last_modified"], (int, float)), "last_modified is epoch float")
    b = projs[1]
    eq(b["session_count"], 1, "subdir sidechain file not counted in session_count")
    eq(b["name"], "repo-b", "project B name from cwd")


def test_projects_missing(_cdir):
    with tempfile.TemporaryDirectory() as empty:
        r = history.main(action="projects", claude_dir=empty)
        eq(r, {"projects": []}, "missing projects dir -> empty list, no error")


def test_sessions(cdir):
    r = history.main(action="sessions", project="-Users-jane-repo-a", claude_dir=cdir)
    sess = r.get("sessions")
    check(isinstance(sess, list), "sessions returns a list")
    files = [s["file"] for s in sess]
    eq(files, ["sess3.jsonl", "sess2.jsonl", "sess1.jsonl"],
       "sessions sorted by last_time desc; zero-message session dropped")

    by = {s["file"]: s for s in sess}
    s1 = by["sess1.jsonl"]
    eq(s1["title"], "Custom Titled Session", "customTitle wins title resolution")
    eq(s1["is_renamed"], True, "customTitle marks is_renamed")
    eq(s1["message_count"], 4, "message_count includes sidechain, excludes meta/skipped")
    eq(s1["sidechain_count"], 1, "one sidechain message counted")
    eq(s1["has_tool_use"], True, "tool_use block detected")
    eq(s1["tokens"], 195, "tokens summed over all assistant usage (incl sidechain)")
    eq(s1["first_time"], "2024-01-01T10:00:00Z", "first_time = first rendered ts")
    eq(s1["last_time"], "2024-01-01T10:04:00Z", "last_time = last rendered ts")

    s2 = by["sess2.jsonl"]
    eq(s2["title"], "My Renamed Session", "local_command rename resolved from content")
    eq(s2["is_renamed"], True, "local_command rename marks is_renamed")
    eq(s2["message_count"], 2, "user + system(local_command) both rendered")
    eq(s2["tokens"], 0, "no assistant usage -> 0 tokens")

    s3 = by["sess3.jsonl"]
    eq(s3["title"], "What is the weather today?",
       "slash-command user skipped; first genuine user text is the title")
    eq(s3["is_renamed"], False, "derived title is not a rename")
    eq(s3["message_count"], 3, "slash-command user still counts as a message")
    eq(s3["has_tool_use"], False, "no tool_use in sess3")


def test_sessions_containment(cdir):
    for bad in ["../etc", "foo/bar", "..", "/abs"]:
        r = history.main(action="sessions", project=bad, claude_dir=cdir)
        check("error" in r, f"path-unsafe project {bad!r} rejected")


def test_messages(cdir):
    r = history.main(action="messages", project="-Users-jane-repo-a",
                     session="sess1.jsonl", claude_dir=cdir)
    eq(r["total"], 3, "default hides sidechains -> 3 messages")
    eq(r["offset"], 0, "offset echoed")
    eq(r["title"], "Custom Titled Session", "messages carries resolved title")
    eq(r["tokens"], 195, "messages carries session token total")
    msgs = r["messages"]
    eq([m["kind"] for m in msgs], ["user", "assistant", "user"], "message kinds/order")
    assistant = msgs[1]
    eq(assistant["model"], "claude-opus-4-1", "assistant model surfaced")
    eq(assistant["is_sidechain"], False, "main-thread message not flagged sidechain")
    btypes = [b["type"] for b in assistant["blocks"]]
    eq(btypes, ["thinking", "text", "tool_use"], "assistant block types/order")
    tu = assistant["blocks"][2]
    eq(tu["name"], "Bash", "tool_use name kept")
    eq(tu["id"], "tool1", "tool_use id kept")
    check('"command": "ls"' in tu["input"], "tool_use input pretty-printed JSON")
    tr = msgs[2]["blocks"][0]
    eq(tr["type"], "tool_result", "tool_result normalized")
    eq(tr["tool_use_id"], "tool1", "tool_result paired id kept")
    eq(tr["text"], "file1\nfile2", "tool_result content flattened to text")
    eq(tr["is_error"], False, "tool_result is_error surfaced")


def test_messages_sidechains(cdir):
    r = history.main(action="messages", project="-Users-jane-repo-a",
                     session="sess1.jsonl", sidechains=True, claude_dir=cdir)
    eq(r["total"], 4, "sidechains=1 includes the subagent message")
    sc = r["messages"][3]
    eq(sc["is_sidechain"], True, "sidechain message flagged")
    eq(sc["blocks"][0]["text"], "subagent reply", "sidechain text present")


def test_messages_pagination(cdir):
    r = history.main(action="messages", project="-Users-jane-repo-a",
                     session="sess1.jsonl", offset=1, limit=1, claude_dir=cdir)
    eq(r["total"], 3, "total is the full filtered count, not the page size")
    eq(len(r["messages"]), 1, "limit bounds the page")
    eq(r["messages"][0]["kind"], "assistant", "offset indexes into the filtered list")


def test_messages_blocks_and_truncation(cdir):
    r = history.main(action="messages", project="-Users-jane-repo-b",
                     session="main.jsonl", claude_dir=cdir)
    eq(r["total"], 2, "project B has 2 messages")
    a = r["messages"][1]
    btypes = [b["type"] for b in a["blocks"]]
    eq(btypes, ["text", "thinking", "image", "text"],
       "redacted_thinking->thinking, image kept as placeholder, order preserved")
    long_block = a["blocks"][0]
    eq(len(long_block["text"]), 10000, "over-long text truncated to 10000 chars")
    eq(long_block["truncated"], True, "truncated flag set")
    eq(a["blocks"][1]["text"], "[redacted]", "redacted_thinking rendered as placeholder")
    eq(a["blocks"][2], {"type": "image"}, "image block is a bare placeholder")


def test_messages_containment_and_missing(cdir):
    r = history.main(action="messages", project="-Users-jane-repo-a",
                     session="../secret.jsonl", claude_dir=cdir)
    check("error" in r, "path-unsafe session rejected")
    r = history.main(action="messages", project="-Users-jane-repo-a",
                     session="does-not-exist.jsonl", claude_dir=cdir)
    check("error" in r, "missing session file returns an error dict, not a crash")


def test_unknown_action(cdir):
    r = history.main(action="bogus", claude_dir=cdir)
    eq(r, {"error": "unknown action"}, "unknown action returns error dict")


def main():
    with tempfile.TemporaryDirectory() as root:
        build_fixture(root)
        test_projects(root)
        test_projects_missing(root)
        test_sessions(root)
        test_sessions_containment(root)
        test_messages(root)
        test_messages_sidechains(root)
        test_messages_pagination(root)
        test_messages_blocks_and_truncation(root)
        test_messages_containment_and_missing(root)
        test_unknown_action(root)
    print(f"\n{PASS} passed, {FAIL} failed")
    sys.exit(1 if FAIL else 0)


if __name__ == "__main__":
    main()
