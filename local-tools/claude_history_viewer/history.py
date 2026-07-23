"""Read-only data module for the Claude Code history viewer.

One `main(action=…)` over `~/.claude/projects/<slug>/<uuid>.jsonl`, dispatching
to three actions — `projects`, `sessions`, `messages`. Stdlib only. Never writes
anything under ~/.claude (files are opened "r" only). Parsing rules live in
specs/history-data.md; the action contracts in specs/architecture.md,
specs/browsing.md, specs/transcript.md.
"""
import json
import os
import re

# system lines whose subtype is rendered as a message (history-data.md §3)
SYSTEM_RENDERED = {"local_command", "compact_boundary", "api_error"}
# per-block text cap before "truncated" (transcript.md §3)
MAX_BLOCK = 10000
# display cap for titles (history-data.md §5)
MAX_TITLE = 120
# how far into the newest file to probe for a cwd (browsing.md §2)
CWD_PROBE_LINES = 100

_RENAME_RE = re.compile(
    r"<local-command-stdout>Session renamed to:\s*(.*?)</local-command-stdout>", re.S)
# text prefixes that mark a user message as a slash-command display / wrapper,
# not genuine user prose (history-data.md §5.3)
_NON_GENUINE = ("<command-name>", "<local-command-stdout>", "<command-message>",
                "<system-reminder>", "Caveat:")


# --------------------------------------------------------------- low-level

def _iter_lines(path):
    """Yield each parseable JSON object dict from a JSONL file. Empty and
    malformed lines are skipped silently (history-data.md §4). Bytes are decoded
    UTF-8 with errors="replace"."""
    with open(path, "r", encoding="utf-8", errors="replace") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except Exception:
                continue
            if isinstance(obj, dict):
                yield obj


def _is_rendered(obj):
    """True if a line is classified 'rendered as a message' (history-data.md §3).
    Assumes isMeta lines are filtered by the caller."""
    t = obj.get("type")
    if t in ("user", "assistant"):
        # skip lines lacking BOTH sessionId and timestamp
        if obj.get("sessionId") is None and obj.get("timestamp") is None:
            return False
        return True
    if t == "system":
        return obj.get("subtype") in SYSTEM_RENDERED
    return False


def _first_text(content):
    """First text payload of a message.content (string or block array)."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        for b in content:
            if isinstance(b, dict) and b.get("type") == "text":
                return b.get("text") or ""
    return ""


def _is_genuine(text):
    t = (text or "").lstrip()
    if not t.strip():
        return False
    return not any(t.startswith(p) for p in _NON_GENUINE)


def _extract_rename(obj):
    """Rename title from a custom-title line or any line carrying the
    'Session renamed to:' local-command stdout (history-data.md §5.1)."""
    if obj.get("type") == "custom-title":
        ct = obj.get("customTitle")
        if ct and str(ct).strip():
            return str(ct).strip()
    content = obj.get("content")
    if not isinstance(content, str):
        msg = obj.get("message")
        if isinstance(msg, dict):
            content = msg.get("content")
    if isinstance(content, str):
        m = _RENAME_RE.search(content)
        if m and m.group(1).strip():
            return m.group(1).strip()
    return None


def _usage_tokens(msg):
    usage = msg.get("usage")
    if not isinstance(usage, dict):
        return 0
    total = 0
    for k in ("input_tokens", "output_tokens",
              "cache_creation_input_tokens", "cache_read_input_tokens"):
        try:
            total += int(usage.get(k, 0) or 0)
        except (TypeError, ValueError):
            pass
    return total


def _resolve_title(rename, summary, user_text, assistant_text, stem):
    """First non-empty wins, in priority order (history-data.md §5). Returns
    (title, is_renamed)."""
    if rename and rename.strip():
        return rename.strip()[:MAX_TITLE], True
    for cand in (summary, user_text, assistant_text):
        if cand and cand.strip():
            return cand.strip()[:MAX_TITLE], False
    return stem, False


# --------------------------------------------------------------- truncation

def _trunc(s):
    """-> (text, full_len) where full_len is 0 unless the text was cut."""
    s = s if isinstance(s, str) else ("" if s is None else str(s))
    if len(s) > MAX_BLOCK:
        return s[:MAX_BLOCK], len(s)
    return s, 0


def _text_block(kind, text):
    t, n = _trunc(text)
    b = {"type": kind, "text": t, "truncated": bool(n)}
    if n:
        b["full_len"] = n
    return b


def _flatten_result(content):
    """tool_result content -> flat text (string, or nested text blocks joined)."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for b in content:
            if isinstance(b, dict):
                if b.get("type") == "text":
                    parts.append(b.get("text") or "")
                elif "text" in b:
                    parts.append(str(b.get("text") or ""))
            elif isinstance(b, str):
                parts.append(b)
        return "\n".join(parts)
    if content is None:
        return ""
    return str(content)


def _norm_block(b):
    """Normalize one content block to the transcript.md §2 wire shape; unknown
    block types return None (dropped)."""
    if not isinstance(b, dict):
        return None
    bt = b.get("type")
    if bt == "text":
        return _text_block("text", b.get("text") or "")
    if bt == "thinking":
        return _text_block("thinking", b.get("thinking") or "")
    if bt == "redacted_thinking":
        return {"type": "thinking", "text": "[redacted]", "truncated": False}
    if bt == "tool_use":
        inp = b.get("input")
        try:
            pretty = json.dumps(inp, indent=2, ensure_ascii=False)
        except (TypeError, ValueError):
            pretty = str(inp)
        text, n = _trunc(pretty)
        out = {"type": "tool_use", "id": b.get("id") or "",
               "name": b.get("name") or "", "input": text, "truncated": bool(n)}
        if n:
            out["full_len"] = n
        return out
    if bt == "tool_result":
        text, n = _trunc(_flatten_result(b.get("content")))
        out = {"type": "tool_result", "tool_use_id": b.get("tool_use_id") or "",
               "text": text, "is_error": bool(b.get("is_error")), "truncated": bool(n)}
        if n:
            out["full_len"] = n
        return out
    if bt == "image":
        return {"type": "image"}
    return None


def _normalize(obj):
    """A rendered line -> a Message dict (transcript.md §2)."""
    t = obj.get("type")
    if t == "system":
        content = obj.get("content")
        text = content if isinstance(content, str) else (
            "" if content is None else json.dumps(content, ensure_ascii=False))
        return {"uuid": obj.get("uuid"), "kind": "system",
                "timestamp": obj.get("timestamp"), "model": None,
                "is_sidechain": bool(obj.get("isSidechain")),
                "subtype": obj.get("subtype"),
                "blocks": [_text_block("text", text)]}
    msg = obj.get("message") or {}
    content = msg.get("content")
    blocks = []
    if isinstance(content, str):
        blocks = [_text_block("text", content)]
    elif isinstance(content, list):
        for b in content:
            nb = _norm_block(b)
            if nb is not None:
                blocks.append(nb)
    return {"uuid": obj.get("uuid"), "kind": t,
            "timestamp": obj.get("timestamp"),
            "model": msg.get("model") if t == "assistant" else None,
            "is_sidechain": bool(obj.get("isSidechain")),
            "subtype": None, "blocks": blocks}


# --------------------------------------------------------------- scans

def _scan_session(path):
    """Single bounded pass for the sessions listing (history-data.md §9). Never
    retains message bodies."""
    rename = summary = user_text = assistant_text = None
    msg_count = side_count = tokens = 0
    has_tool = False
    first_time = last_time = None
    for obj in _iter_lines(path):
        if rename is None:
            r = _extract_rename(obj)
            if r:
                rename = r
        if summary is None and obj.get("type") == "summary":
            s = obj.get("summary")
            if s and str(s).strip():
                summary = str(s)
        if obj.get("isMeta") is True:
            continue
        if not _is_rendered(obj):
            continue
        msg_count += 1
        if obj.get("isSidechain") is True:
            side_count += 1
        ts = obj.get("timestamp")
        if ts:
            if first_time is None:
                first_time = ts
            last_time = ts
        t = obj.get("type")
        msg = obj.get("message") or {}
        if t == "assistant":
            tokens += _usage_tokens(msg)
            content = msg.get("content")
            if isinstance(content, list) and any(
                    isinstance(b, dict) and b.get("type") == "tool_use"
                    for b in content):
                has_tool = True
            if assistant_text is None:
                txt = _first_text(content)
                if txt.strip():
                    assistant_text = txt
        elif t == "user" and user_text is None:
            txt = _first_text(msg.get("content"))
            if _is_genuine(txt):
                user_text = txt
    stem = os.path.splitext(os.path.basename(path))[0]
    title, is_renamed = _resolve_title(rename, summary, user_text,
                                       assistant_text, stem)
    return {"title": title, "is_renamed": is_renamed,
            "message_count": msg_count, "sidechain_count": side_count,
            "first_time": first_time, "last_time": last_time,
            "has_tool_use": has_tool, "tokens": tokens}


def _probe_cwd(path):
    """First non-empty cwd within the first CWD_PROBE_LINES lines."""
    n = 0
    for obj in _iter_lines(path):
        cwd = obj.get("cwd")
        if cwd and str(cwd).strip():
            return str(cwd)
        n += 1
        if n >= CWD_PROBE_LINES:
            break
    return None


def _decode_slug(slug):
    """Lossy slug -> path fallback ('-' -> '/'), best effort (browsing.md §2)."""
    return slug.replace("-", "/")


# --------------------------------------------------------------- containment

def _bad_key(value):
    """Reject anything that isn't a plain slug/filename — the only accepted key
    (architecture.md §2). Returns an error dict or None."""
    if not value:
        return {"error": "missing name"}
    if os.path.isabs(value) or "/" in value or "\\" in value or ".." in value:
        return {"error": "invalid name"}
    return None


# --------------------------------------------------------------- actions

def _projects(base):
    pdir = os.path.join(base, "projects")
    if not os.path.isdir(pdir):
        return {"projects": []}
    out = []
    try:
        names = os.listdir(pdir)
    except OSError as e:
        return {"error": f"cannot list projects: {e}"}
    for name in names:
        d = os.path.join(pdir, name)
        # a project dir can vanish or lose read permission mid-scan; skip, don't die
        try:
            if not os.path.isdir(d):
                continue
            jsonls = [f for f in os.listdir(d)
                      if f.endswith(".jsonl") and os.path.isfile(os.path.join(d, f))]
            if not jsonls:
                continue
            mtimes = {f: os.path.getmtime(os.path.join(d, f)) for f in jsonls}
        except OSError:
            continue
        newest = max(jsonls, key=lambda f: mtimes[f])
        try:
            path = _probe_cwd(os.path.join(d, newest))
        except OSError:
            path = None
        if not path:
            path = _decode_slug(name)
        leaf = os.path.basename(path.rstrip("/")) or name
        out.append({"slug": name, "name": leaf, "path": path,
                    "session_count": len(jsonls),
                    "last_modified": max(mtimes.values())})
    out.sort(key=lambda p: p["last_modified"], reverse=True)
    return {"projects": out}


def _sessions(base, project):
    bad = _bad_key(project)
    if bad:
        return bad
    d = os.path.join(base, "projects", project)
    if not os.path.isdir(d):
        return {"error": "project not found"}
    sessions = []
    for f in sorted(os.listdir(d)):
        full = os.path.join(d, f)
        if not (f.endswith(".jsonl") and os.path.isfile(full)):
            continue
        try:
            meta = _scan_session(full)
        except OSError:
            continue
        if meta["message_count"] == 0:
            continue
        meta["file"] = f
        sessions.append(meta)
    sessions.sort(key=lambda s: (s.get("last_time") or ""), reverse=True)
    return {"sessions": sessions}


def _messages(base, project, session, offset, limit, include_side):
    bad = _bad_key(project) or _bad_key(session)
    if bad:
        return bad
    full = os.path.join(base, "projects", project, session)
    if not os.path.isfile(full):
        return {"error": "session not found"}

    rename = summary = user_text = assistant_text = None
    tokens = 0
    messages = []
    try:
        for obj in _iter_lines(full):
            if rename is None:
                r = _extract_rename(obj)
                if r:
                    rename = r
            if summary is None and obj.get("type") == "summary":
                s = obj.get("summary")
                if s and str(s).strip():
                    summary = str(s)
            if obj.get("isMeta") is True:
                continue
            if not _is_rendered(obj):
                continue
            t = obj.get("type")
            msg = obj.get("message") or {}
            if t == "assistant":
                tokens += _usage_tokens(msg)
                if assistant_text is None:
                    txt = _first_text(msg.get("content"))
                    if txt.strip():
                        assistant_text = txt
            elif t == "user" and user_text is None:
                txt = _first_text(msg.get("content"))
                if _is_genuine(txt):
                    user_text = txt
            if obj.get("isSidechain") is True and not include_side:
                continue
            messages.append(_normalize(obj))
    except OSError as e:
        return {"error": f"cannot open session: {e}"}

    stem = os.path.splitext(os.path.basename(full))[0]
    title, _ = _resolve_title(rename, summary, user_text, assistant_text, stem)
    total = len(messages)
    if offset < 0:
        offset = 0
    page = messages[offset:offset + limit] if limit >= 0 else messages[offset:]
    return {"messages": page, "total": total, "offset": offset,
            "title": title, "tokens": tokens}


def main(action: str = "projects", project: str = "", session: str = "",
         offset: int = 0, limit: int = 200, sidechains: bool = False,
         claude_dir: str = ""):
    base = claude_dir or os.path.expanduser("~/.claude")
    if action == "projects":
        return _projects(base)
    if action == "sessions":
        return _sessions(base, project)
    if action == "messages":
        return _messages(base, project, session, int(offset), int(limit),
                         bool(sidechains))
    return {"error": "unknown action"}
