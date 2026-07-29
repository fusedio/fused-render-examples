"""SSH connection daemon for the VPS Manager — stdlib only, no FastAPI, no uvicorn.

Same application as examples/vps_manager (same UI, same endpoint contract); the
only difference is underneath. This backend is the stdlib daemon pattern the rest
of the repo already uses (db_console, the geotiff/map/netcdf tile servers):
ThreadingHTTPServer plus a hand-rolled RFC 6455 WebSocket for the terminal, with
paramiko as the single third-party import. Kept as its own folder so the two can
be timed against each other — importing fastapi+uvicorn costs ~3.0s on a cold
daemon start, the stdlib equivalent ~0.55s.

Each fused-render runPython call is a fresh subprocess, so SSH sessions can't
live there and a resident daemon is unavoidable: a cold connect measures ~8.5s
(interpreter + paramiko import + SSH handshake) against ~0.9s on a warm one.
This module is therefore both:

  1. a runPython entrypoint `main(action="ensure")` — starts (or reuses) a
     long-lived localhost daemon and returns its port and token; and
  2. the daemon (run as `python vps.py --serve`) — holds live paramiko
     connections per machine and serves HTTP + a terminal WebSocket.

Endpoints (CORS *; every route but /ping requires `?t=<token>`):
  GET  /ping                       -> {"ok", "version"}
  GET  /quit
  GET  /machines                   -> {"machines": [{id, name, host, port,
                                       username, key_path, source, connected}],
                                       config_path, config_error, hidden}
  POST /machines/add               -> body {name, host, port, username,
                                       key_path, password} -> {machine}
  POST /machines/{id}/update       -> same body -> {machine}
  POST /machines/{id}/remove       -> {"ok"}
  POST /machines/{id}/hide         -> {"ok"}   (ssh-config hosts only)
  POST /machines/{id}/unhide       -> {"ok"}
  GET  /known?limit=               -> {hosts: [machine], total, hashed, path}
  POST /connect?id=                -> {"ok", "home", "sftp"}  (400 with reason)
  POST /probe?id=                  -> {"ok", "ms", "shell", "sftp", "detail"}
  POST /reach?id=                  -> {"ok", "ms", "banner"}  (TCP only, no login)
  POST /disconnect?id=             -> {"ok"}
  GET  /ls?id=&path=               -> {path, parent, home, entries: [{name,
                                       is_dir, is_link, size, mtime}]}
  GET  /search?id=&path=&q=        -> {entries: [...], truncated}
  POST /mkdir?id=&path=            -> {"ok"}
  POST /rename?id=&src=&dst=       -> {"ok"}   (also move)
  POST /copy?id=&src=&dst=         -> {"ok"}
  POST /delete?id=&path=           -> {"ok"}
  GET  /download?id=&path=         -> file bytes
  GET  /localize?id=&path=         -> {local_path, name, size}  (cache for preview)
  POST /upload?id=&dir=&name=      -> raw request body is the file -> {"ok"}
  WS   /term?id=&cols=&rows=       -> in: {"data"} | {"resize": [c, r]},
                                       out: raw shell output text

The machine list is ~/.ssh/config — plus /etc/ssh/ssh_config, or %ProgramData%/ssh
on Windows — read live, ids `cfg:<alias>`, Include directives expanded, first
setting wins as in ssh; plus anything added by hand in machines.json beside this
file (passwords included — prefer key_path). A hand-added machine on the same
user@host:port as a config alias supersedes it. /known adds the hosts that only
~/.ssh/known_hosts remembers (ids `kh:<host>:<port>`, username guessed) so a box
rented for an afternoon can be picked up again. Idle shutdown after 30 min with
no requests and no open terminals. The state file embeds a hash of this module,
so editing it respawns a fresh daemon on the next ensure().
"""
# /// script
# dependencies = ["paramiko>=3"]
# ///
import glob
import hashlib
import json
import os
import posixpath
import sys
import threading
import time

IDLE_EXIT_S = 30 * 60
STATE = os.path.expanduser("~/.cache/fused-render-vps-stdlib-v1/daemon.json")
PREVIEW_DIR = os.path.expanduser("~/.cache/fused-render-vps-stdlib-v1/preview")
HERE = os.path.dirname(os.path.abspath(__file__)) if "__file__" in globals() else os.path.abspath(sys.path[0])
MACHINES = os.path.join(HERE, "machines.json")

# OpenSSH keeps per-user files in ~/.ssh on macOS, Linux and Windows alike
# (expanduser falls back to %USERPROFILE% there). Only the system-wide directory
# moves: /etc/ssh on unix, %ProgramData%/ssh for Windows' OpenSSH port.
SSH_DIR = os.path.join(os.path.expanduser("~"), ".ssh")
SSH_CONFIG = os.path.join(SSH_DIR, "config")
KNOWN_HOSTS = os.path.join(SSH_DIR, "known_hosts")
SYSTEM_SSH_DIR = (os.path.join(os.environ.get("ProgramData", r"C:\ProgramData"), "ssh")
                  if os.name == "nt" else "/etc/ssh")
SYSTEM_CONFIG = os.path.join(SYSTEM_SSH_DIR, "ssh_config")
SYSTEM_KNOWN_HOSTS = os.path.join(SYSTEM_SSH_DIR, "ssh_known_hosts")


class Http(Exception):
    """Mirrors FastAPI's HTTPException shape so the page's error handling is
    unchanged: the body is {"detail": ...} at the given status."""

    def __init__(self, code, detail):
        super().__init__(detail)
        self.code = code
        self.detail = detail


def _me():
    return os.path.join(HERE, "vps.py")


def _version():
    """Hash the code, not its mtime plus the interpreter path — a version that
    moves with either churns the daemon on every checkout or interpreter switch."""
    try:
        with open(_me(), "rb") as f:
            return hashlib.sha256(f.read()).hexdigest()[:12]
    except OSError:
        return "0"


def _daemon_executable():
    """Prefer the windowless pythonw.exe so no console can flash on Windows."""
    if os.name == "nt":
        pyw = os.path.join(os.path.dirname(sys.executable), "pythonw.exe")
        if os.path.exists(pyw):
            return pyw
    return sys.executable


# ================================================================ ssh config
def _config_text(path, depth=0):
    """ssh_config text with Include directives inlined — paramiko doesn't do them.
    A relative Include resolves against the directory of the file naming it, so
    ~/.ssh/config picks up ~/.ssh/conf.d/* and /etc/ssh/ssh_config /etc/ssh/*."""
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            lines = f.readlines()
    except OSError:
        return ""
    if depth >= 5:
        return "".join(lines)
    out = []
    for line in lines:
        parts = line.strip().split(None, 1)
        if len(parts) == 2 and parts[0].lower() == "include":
            for pat in parts[1].split():
                pat = os.path.expanduser(pat.strip('"'))
                if not os.path.isabs(pat):
                    pat = os.path.join(os.path.dirname(path), pat)
                for inc in sorted(glob.glob(pat)):
                    out.append(_config_text(inc, depth + 1))
        else:
            out.append(line)
    return "".join(out)


def _parse_config():
    """(paramiko.SSHConfig, [alias]) — aliases in file order, wildcards dropped.

    The user's config comes first and the system one after, because both ssh and
    paramiko keep the first value they find for a setting."""
    import io

    import paramiko
    text = _config_text(SSH_CONFIG) + "\n" + _config_text(SYSTEM_CONFIG)
    cfg = paramiko.SSHConfig()
    cfg.parse(io.StringIO(text))
    aliases, seen = [], set()
    for line in text.splitlines():
        parts = line.strip().split(None, 1)
        if len(parts) == 2 and parts[0].lower() == "host":
            for a in parts[1].replace('"', "").split():
                if any(c in a for c in "*?!") or a in seen:
                    continue
                seen.add(a)
                aliases.append(a)
    return cfg, aliases


def config_machines():
    """Machines described by ~/.ssh/config, in the order they appear there."""
    import getpass
    cfg, aliases = _parse_config()
    out = []
    for alias in aliases:
        h = cfg.lookup(alias)
        keys = [os.path.expanduser(k) for k in h.get("identityfile") or []]
        out.append({"id": "cfg:" + alias,
                    "name": alias,
                    "host": h.get("hostname") or alias,
                    "port": int(h.get("port") or 22),
                    "username": h.get("user") or getpass.getuser(),
                    "key_path": keys[0] if keys else "",
                    "key_paths": keys,
                    "proxy_jump": h.get("proxyjump") or "",
                    "proxy_command": h.get("proxycommand") or "",
                    "source": "ssh_config"})
    return out


# ================================================================ known_hosts
def ssh_private_keys(limit=6):
    """Private keys in ~/.ssh, newest first — a box rented last week was almost
    certainly reached with a recent key. Capped because a guessed host tries them
    one connection at a time, and that time adds up."""
    out = []
    for name in sorted(os.listdir(SSH_DIR)) if os.path.isdir(SSH_DIR) else []:
        p = os.path.join(SSH_DIR, name)
        if name.endswith(".pub") or not os.path.isfile(p):
            continue
        try:
            with open(p, "rb") as f:
                head = f.read(64)
        except OSError:
            continue                   # unix: a key can be there but not ours to read
        if b"PRIVATE KEY" in head:
            out.append(p)
    out.sort(key=os.path.getmtime, reverse=True)
    return out[:limit]


def _host_port(token):
    """`host`, `[host]:port` -> (host, port)."""
    if token.startswith("["):
        host, _, port = token[1:].partition("]:")
        return host, int(port) if port.isdigit() else 22
    return token, 22


def _known_machine(cfg, keys, host, port):
    h = cfg.lookup(host)      # the file can key an entry by alias as well as by IP
    return {"id": f"kh:{host}:{port}",
            "name": host,
            "host": h.get("hostname") or host,
            "port": port,
            "username": h.get("user") or "root",
            "key_path": "",
            "key_paths": [os.path.expanduser(k) for k in h.get("identityfile") or []] or keys,
            "proxy_jump": h.get("proxyjump") or "",
            "proxy_command": h.get("proxycommand") or "",
            "source": "known_hosts",
            "guessed": True}


def known_machine(host, port):
    return _known_machine(_parse_config()[0], ssh_private_keys(), host, port)


def known_hosts_files(cfg):
    """The host-key files ssh would read, system ones first so the user's file owns
    the recent end. GlobalKnownHostsFile/UserKnownHostsFile override the defaults;
    entries using ssh's % tokens are left to ssh."""
    out = []
    for setting, default in (("globalknownhostsfile", SYSTEM_KNOWN_HOSTS),
                             ("userknownhostsfile", KNOWN_HOSTS)):
        named = (cfg.lookup("*").get(setting) or "").split()
        out += [os.path.expanduser(p) for p in named if "%" not in p] or [default]
    return out


def known_hosts_machines(limit=25, taken=()):
    """(machines, total, hashed) from the known_hosts files, most recent first.

    known_hosts stores no username, no key and no timestamp, so the username here
    is a guess and "most recent" means "nearest the end of the file" — the order
    ssh appends in. Hashed (|1|…) entries can't be reversed, so they're only counted.
    """
    cfg, _ = _parse_config()
    seen, total, hashed = {}, 0, 0
    for path in known_hosts_files(cfg):
        try:
            with open(path, encoding="utf-8", errors="replace") as f:
                lines = f.readlines()
        except OSError:
            continue
        for line in lines:
            parts = line.split()
            if parts and parts[0].startswith("@"):
                parts = parts[1:]
            if not parts or parts[0].startswith("#"):
                continue
            total += 1
            if parts[0].startswith("|"):
                hashed += 1
                continue
            for token in parts[0].split(","):
                if not token or any(c in token for c in "*?"):
                    continue
                host, port = _host_port(token)
                seen.pop((host.lower(), port), None)      # re-seen == more recent
                seen[(host.lower(), port)] = (host, port)
    keys = ssh_private_keys()
    out = []
    for host, port in reversed(list(seen.values())):
        if (host.lower(), port) in taken:
            continue
        out.append(_known_machine(cfg, keys, host, port))
        if len(out) >= limit:
            break
    return out, total, hashed


# ================================================================ ensure()
def _alive(port, token, version):
    import urllib.request
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/ping", timeout=2) as r:
            d = json.load(r)
        return d.get("ok") and d.get("version") == version and token
    except Exception:
        return False


def main(action: str = "ensure"):
    """runPython entrypoint: make sure the daemon is running, return {port, token}."""
    import importlib.util
    import subprocess
    # find_spec, not `import paramiko` — this runs on every page load, and actually
    # importing it costs ~1.5s for a check the daemon's own log would report anyway.
    if importlib.util.find_spec("paramiko") is None:
        return {"error": "paramiko is not installed — run: uv pip install paramiko "
                         f"--python {sys.executable}"}
    version = _version()
    try:
        with open(STATE) as f:
            st = json.load(f)
        if _alive(st.get("port"), st.get("token"), version):
            return {"port": st["port"], "token": st["token"], "reused": True,
                    "version": version}
        try:
            import urllib.request
            urllib.request.urlopen(
                f"http://127.0.0.1:{st.get('port')}/quit?t={st.get('token', '')}",
                timeout=1).read()
        except Exception:
            pass
    except (OSError, ValueError):
        pass

    os.makedirs(os.path.dirname(STATE), exist_ok=True)
    log = os.path.join(os.path.dirname(STATE), "daemon.log")
    # DETACHED_PROCESS and CREATE_NO_WINDOW are conflicting console flags — the
    # proven templates (docs, latex, usd) detach with the process-group pair only,
    # and CREATE_NO_WINDOW belongs on blocking console children instead.
    detach = ({"creationflags": subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP}
              if os.name == "nt" else {"start_new_session": True})
    with open(log, "ab") as lf:
        subprocess.Popen([_daemon_executable(), _me(), "--serve"],
                         stdout=lf, stderr=lf, stdin=subprocess.DEVNULL,
                         cwd=HERE, close_fds=True, **detach)
    for _ in range(200):
        time.sleep(0.05)
        try:
            with open(STATE) as f:
                st = json.load(f)
            if st.get("version") == version and _alive(st.get("port"), st.get("token"), version):
                return {"port": st["port"], "token": st["token"], "reused": False,
                        "version": version}
        except (OSError, ValueError):
            continue
    return {"error": f"daemon did not start — see {log}"}


# ================================================================ websocket
WS_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"      # RFC 6455 §1.3


def _ws_accept(key):
    return __import__("base64").b64encode(
        hashlib.sha1((key + WS_GUID).encode()).digest()).decode()


def _ws_frame(payload, opcode=0x1):
    """Server->client frame: never masked, FIN always set (we don't fragment)."""
    import struct
    n = len(payload)
    if n < 126:
        head = struct.pack("!BB", 0x80 | opcode, n)
    elif n < 65536:
        head = struct.pack("!BBH", 0x80 | opcode, 126, n)
    else:
        head = struct.pack("!BBQ", 0x80 | opcode, 127, n)
    return head + payload


def _ws_read(rfile):
    """Read one client frame -> (opcode, payload); (None, b"") at EOF.

    Continuation frames are reassembled, because a browser is free to fragment
    even the small JSON messages the terminal sends."""
    import struct
    data, first = b"", None
    while True:
        head = rfile.read(2)
        if len(head) < 2:
            return None, b""
        b0, b1 = head[0], head[1]
        fin, opcode, masked, n = b0 & 0x80, b0 & 0x0F, b1 & 0x80, b1 & 0x7F
        if n == 126:
            n = struct.unpack("!H", rfile.read(2))[0]
        elif n == 127:
            n = struct.unpack("!Q", rfile.read(8))[0]
        mask = rfile.read(4) if masked else b""
        chunk = rfile.read(n) if n else b""
        if len(chunk) < n:
            return None, b""
        if masked:
            chunk = bytes(c ^ mask[i % 4] for i, c in enumerate(chunk))
        if first is None:
            first = opcode
        if opcode in (0x8, 0x9, 0xA):      # control frames are never fragmented
            return opcode, chunk
        data += chunk
        if fin:
            return first, data


# ================================================================ daemon
def _serve():
    import getpass
    import secrets
    import shlex
    import socket
    import stat as statmod
    import uuid
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    from urllib.parse import parse_qs, urlparse

    import paramiko

    VERSION = _version()
    TOKEN = secrets.token_urlsafe(32)
    last_hit = [time.time()]
    open_terms = [0]

    # ---- machine registry ----
    reg_lock = threading.Lock()

    def load_registry():
        try:
            with open(MACHINES, encoding="utf-8") as f:
                reg = json.load(f)
        except (OSError, ValueError):
            reg = {}
        reg.setdefault("machines", [])
        reg.setdefault("hidden", [])
        return reg

    def load_machines():
        return load_registry()["machines"]

    def save_registry(reg):
        tmp = MACHINES + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(reg, f, indent=2)
        os.replace(tmp, MACHINES)

    def save_machines(machines):
        reg = load_registry()
        reg["machines"] = machines
        save_registry(reg)

    def endpoint(m):
        return (m["host"].lower(), int(m.get("port") or 22), m["username"])

    def all_machines():
        """ssh_config hosts + hand-added machines. A hand-added machine on the same
        user@host:port supersedes the config alias, which it then links back to."""
        reg = load_registry()
        added = reg["machines"]
        try:
            cfg = config_machines()
            err = ""
        except Exception as e:
            cfg, err = [], str(e) or type(e).__name__
        by_endpoint = {endpoint(m): m for m in added}
        out, hidden = [], []
        for m in cfg:
            twin = by_endpoint.get(endpoint(m))
            if twin is not None:
                twin["from_config"] = m["name"]
                continue
            (hidden if m["name"] in reg["hidden"] else out).append(m)
        return out + added, hidden, err

    def get_machine(mid):
        if mid.startswith("kh:"):
            host, _, port = mid[3:].rpartition(":")
            if host and port.isdigit():
                return known_machine(host, int(port))
        pool, hidden, _ = all_machines()
        for m in pool + hidden:
            if m["id"] == mid:
                return m
        raise Http(404, f"unknown machine {mid}")

    def public(m):
        return {k: v for k, v in m.items() if k != "password"}

    # ---- live connections ----
    conns = {}            # id -> {"client", "jump", "sftp", "home", "key_used", "lock"}
    conns_lock = threading.Lock()
    dials = {}            # id -> Lock serialising the dial for that one machine

    def drop_conn(mid):
        with conns_lock:
            c = conns.pop(mid, None)
        if c:
            for key in ("client", "jump"):
                try:
                    if c.get(key):
                        c[key].close()
                except Exception:
                    pass

    def is_connected(mid):
        c = conns.get(mid)
        if not c:
            return False
        t = c["client"].get_transport()
        return bool(t and t.is_active())

    def keys_of(m):
        paths = m.get("key_paths") or ([m["key_path"]] if m.get("key_path") else [])
        return [p for p in (os.path.expanduser(k) for k in paths) if os.path.isfile(p)]

    def jump_machine(spec):
        """Resolve a ProxyJump target ([user@]host[:port]) through ~/.ssh/config."""
        cfg, _ = _parse_config()
        user, _, rest = spec.rpartition("@")
        host, _, port = rest.partition(":")
        h = cfg.lookup(host)
        return {"name": spec, "host": h.get("hostname") or host,
                "port": int(port or h.get("port") or 22),
                "username": user or h.get("user") or getpass.getuser(),
                "key_paths": [os.path.expanduser(k) for k in h.get("identityfile") or []]}

    def open_client(m, depth=0):
        """Connect to one machine, hopping through its ProxyJump if it has one.
        Returns (client, jump_client_or_None, key_that_worked).

        A known_hosts guess has no recorded identity, so its keys are offered one
        at a time — slower, but then we know which one to save."""
        if m.get("proxy_command"):
            raise Http(400, "ProxyCommand hosts aren't supported — "
                            "add the machine by hand instead")
        port = int(m.get("port") or 22)
        keys = keys_of(m)
        pw = m.get("password") or None
        attempts = [None] + [[k] for k in keys] if m.get("guessed") else [keys or None]
        jump, err = None, None
        for keyfile in attempts:
            sock = None
            if m.get("proxy_jump"):
                if depth >= 3:
                    raise Http(400, "ProxyJump chain is too long")
                if jump is None:
                    jump = open_client(jump_machine(m["proxy_jump"]), depth + 1)[0]
                sock = jump.get_transport().open_channel(
                    "direct-tcpip", (m["host"], port), ("127.0.0.1", 0))
            client = paramiko.SSHClient()
            client.load_system_host_keys()
            client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
            try:
                client.connect(m["host"], port=port, username=m["username"],
                               key_filename=keyfile, password=pw, sock=sock,
                               look_for_keys=not (keyfile or pw), allow_agent=not pw,
                               timeout=8, auth_timeout=12, banner_timeout=12)
            except paramiko.AuthenticationException as e:
                err = e
                client.close()
                continue
            except Exception:
                if jump:
                    jump.close()
                raise
            client.get_transport().set_keepalive(20)
            return client, jump, (keyfile[0] if keyfile else "")
        if jump:
            jump.close()
        raise err

    def dial_lock(mid):
        with conns_lock:
            lk = dials.get(mid)
            if lk is None:
                lk = dials[mid] = threading.Lock()
            return lk

    def get_conn(mid):
        """The live connection for one machine, dialling it if there isn't one.

        The dial is serialised per machine: the page fires /probe for every row
        and /connect for the one you clicked at the same time, and without this
        both would open their own SSH session, the second overwriting — and so
        leaking — the first."""
        if is_connected(mid):
            return conns[mid]
        with dial_lock(mid):
            if is_connected(mid):      # another thread dialled while we waited
                return conns[mid]
            m = get_machine(mid)
            drop_conn(mid)
            client, jump, key_used = open_client(m)
            try:
                sftp = client.open_sftp()
                home = sftp.normalize(".")
            except Exception:
                sftp, home = None, "/"   # git-only hosts authenticate but refuse SFTP
            c = {"client": client, "jump": jump, "sftp": sftp, "home": home,
                 "key_used": key_used, "username": m["username"], "lock": threading.Lock(),
                 "tsftp": None, "tlock": threading.Lock(), "gnu_find": None}
            with conns_lock:
                conns[mid] = c
            return c

    def sftp_of(c):
        if c["sftp"] is None:
            raise Http(400, "this host allows SSH but not SFTP, so there are "
                            "no files to browse — try the terminal")
        return c["sftp"]

    def tsftp(c):
        """A second SFTP channel for bulk transfers, so streaming a big download or
        upload never blocks directory listings on c["lock"]. Call under c["tlock"]."""
        sftp_of(c)
        t = c.get("tsftp")
        if t is None or t.sock.closed:
            t = c["tsftp"] = c["client"].open_sftp()
        return t

    def sh(c, cmd):
        _, out, err = c["client"].exec_command(cmd, timeout=60)
        rc = out.channel.recv_exit_status()
        if rc != 0:
            msg = err.read().decode("utf-8", "replace").strip()
            raise Http(400, msg or f"command failed (exit {rc})")
        return out.read().decode("utf-8", "replace")

    def sh_soft(c, cmd):
        _, out, _ = c["client"].exec_command(cmd, timeout=60)
        data = out.read().decode("utf-8", "replace")
        out.channel.recv_exit_status()
        return data

    def sh_rc(c, cmd):
        """Just the exit status — for asking a remote what its tools can do."""
        _, out, _ = c["client"].exec_command(cmd, timeout=60)
        out.read()
        return out.channel.recv_exit_status()

    def gnu_find(c):
        """Does this host's `find` support GNU -printf? BSD (macOS) and older
        BusyBox (Alpine) don't, and would otherwise fail the whole search.
        -maxdepth 0 walks nothing, so the probe is instant; cached per
        connection."""
        if c["gnu_find"] is None:
            c["gnu_find"] = sh_rc(c, "find . -maxdepth 0 -printf '' 2>/dev/null") == 0
        return c["gnu_find"]

    # ---- endpoints ----
    def do_machines():
        pool, hidden, err = all_machines()
        return {"machines": [dict(public(m), connected=is_connected(m["id"]))
                             for m in pool],
                "hidden": [public(m) for m in hidden],
                "config_path": SSH_CONFIG if os.path.isfile(SSH_CONFIG) else "",
                "config_error": err}

    def do_known(limit):
        pool, hidden, _ = all_machines()
        taken = set()
        for m in pool + hidden:      # known_hosts keys entries by alias or hostname
            taken.add((m["host"].lower(), int(m.get("port") or 22)))
            taken.add((m["name"].lower(), int(m.get("port") or 22)))
        hosts, total, hashed = known_hosts_machines(limit, taken)
        paths = [p for p in known_hosts_files(_parse_config()[0]) if os.path.isfile(p)]
        return {"hosts": hosts, "total": total, "hashed": hashed,
                "path": "\n".join(paths)}

    def do_add(body):
        m = {"id": uuid.uuid4().hex[:8],
             "name": (body.get("name") or body.get("host") or "").strip(),
             "host": (body.get("host") or "").strip(),
             "port": int(body.get("port") or 22),
             "username": (body.get("username") or "").strip(),
             "key_path": (body.get("key_path") or "").strip(),
             "password": body.get("password") or ""}
        if not m["host"] or not m["username"]:
            raise Http(400, "host and username are required")
        with reg_lock:
            ms = load_machines()
            ms.append(m)
            save_machines(ms)
        return {"machine": public(m)}

    def cfg_alias(mid):
        if not mid.startswith("cfg:"):
            raise Http(400, "not an ssh-config host")
        return mid[4:]

    def reject_derived(mid, verb):
        if mid.startswith("cfg:"):
            raise Http(400, f"{mid[4:]} comes from {SSH_CONFIG} — edit that "
                            f"file to {verb} it, or hide it from this list")
        if mid.startswith("kh:"):
            raise Http(400, "this host is only remembered in known_hosts — "
                            "save it here first")

    def do_update(mid, body):
        reject_derived(mid, "change")
        with reg_lock:
            ms = load_machines()
            for m in ms:
                if m["id"] == mid:
                    for k in ("name", "host", "username", "key_path"):
                        if k in body:
                            m[k] = (body.get(k) or "").strip()
                    if "port" in body:
                        m["port"] = int(body.get("port") or 22)
                    if body.get("password"):
                        m["password"] = body["password"]
                    save_machines(ms)
                    drop_conn(mid)
                    return {"machine": public(m)}
        raise Http(404, f"unknown machine {mid}")

    def do_remove(mid):
        reject_derived(mid, "remove")
        drop_conn(mid)
        with reg_lock:
            save_machines([m for m in load_machines() if m["id"] != mid])
        return {"ok": True}

    def do_hide(mid):
        alias = cfg_alias(mid)
        drop_conn(mid)
        with reg_lock:
            reg = load_registry()
            if alias not in reg["hidden"]:
                reg["hidden"].append(alias)
                save_registry(reg)
        return {"ok": True}

    def do_unhide(mid):
        alias = cfg_alias(mid)
        with reg_lock:
            reg = load_registry()
            reg["hidden"] = [a for a in reg["hidden"] if a != alias]
            save_registry(reg)
        return {"ok": True}

    def do_connect(mid):
        try:
            c = get_conn(mid)
        except Http:
            raise
        except Exception as e:
            drop_conn(mid)
            raise Http(400, str(e) or type(e).__name__)
        return {"ok": True, "home": c["home"], "sftp": c["sftp"] is not None}

    def do_probe(mid):
        """Actually log in and report what answered — never raises for a dead host."""
        t0 = time.time()
        try:
            c = get_conn(mid)
        except Http as e:
            return {"ok": False, "error": e.detail}
        except Exception as e:
            drop_conn(mid)
            return {"ok": False, "error": str(e) or type(e).__name__}
        ms = int((time.time() - t0) * 1000)
        detail, shell = "", False
        try:
            out = sh_soft(c, "echo SSHOK; (. /etc/os-release 2>/dev/null; echo "
                             "${PRETTY_NAME:-}); uname -sr; uptime -p 2>/dev/null")
        except Exception:
            out = ""
        lines = [x.strip() for x in out.splitlines()]
        if lines[:1] == ["SSHOK"]:
            shell = True
            os_name = (lines[1] if len(lines) > 1 else "") or (lines[2] if len(lines) > 2 else "")
            uptime = (lines[3] if len(lines) > 3 else "").split(",")[0]
            detail = " · ".join(x for x in (os_name, uptime) if x)
        return {"ok": True, "ms": ms, "shell": shell, "detail": detail,
                "sftp": c["sftp"] is not None,
                "username": c["username"], "key_path": c["key_used"]}

    def do_reach(mid):
        """Is anything listening? TCP connect and read the SSH banner, no login."""
        m = get_machine(mid)
        t0 = time.time()
        try:
            s = socket.create_connection((m["host"], int(m.get("port") or 22)), timeout=4)
        except OSError as e:
            return {"ok": False, "error": e.strerror or str(e)}
        try:
            s.settimeout(4)
            banner = s.recv(255).decode("utf-8", "replace").strip().splitlines()
        except OSError:
            banner = []
        finally:
            s.close()
        version = banner[0].replace("SSH-2.0-", "").split()[0] if banner else ""
        return {"ok": True, "ms": int((time.time() - t0) * 1000), "banner": version}

    def do_ls(mid, path):
        c = get_conn(mid)
        path = path or c["home"]
        with c["lock"]:
            sftp = sftp_of(c)
            # normalize() is a server round-trip — only pay it for relative paths
            path = posixpath.normpath(path) if path.startswith("/") else sftp.normalize(path)
            entries = []
            for a in sftp.listdir_attr(path):
                is_link = statmod.S_ISLNK(a.st_mode or 0)
                is_dir = statmod.S_ISDIR(a.st_mode or 0)
                if is_link:
                    try:
                        is_dir = statmod.S_ISDIR(sftp.stat(posixpath.join(path, a.filename)).st_mode)
                    except OSError:
                        is_dir = False
                entries.append({"name": a.filename, "is_dir": is_dir,
                                "is_link": is_link, "size": a.st_size or 0,
                                "mtime": a.st_mtime or 0})
        entries.sort(key=lambda e: (not e["is_dir"], e["name"].lower()))
        parent = posixpath.dirname(path.rstrip("/")) or "/"
        return {"path": path, "parent": parent if path != "/" else None,
                "home": c["home"], "entries": entries}

    def do_search(mid, path, q, limit):
        """Recursive name search under one folder, like the Ubuntu file manager.

        GNU find reports type, size and mtime in the same pass via -printf. Where
        that isn't supported, a second -print pass marks the directories: paths
        and folder-ness are POSIX, size and mtime aren't, so those come back blank
        rather than the whole search reporting nothing found."""
        q = q.strip()
        if not q:
            return {"entries": [], "truncated": False}
        c = get_conn(mid)
        find = (f"find {shlex.quote(path)} -maxdepth 8 "
                f"-iname {shlex.quote('*' + q + '*')}")
        cap = f"2>/dev/null | head -n {int(limit)}"
        found = []            # (path, is_dir, is_link, size, mtime)
        if gnu_find(c):
            out = sh_soft(c, f"{find} -printf '%y\\t%s\\t%T@\\t%p\\n' {cap}")
            for line in out.splitlines():
                parts = line.split("\t", 3)
                if len(parts) != 4:
                    continue
                typ, size, mtime, p = parts
                found.append((p, typ == "d", typ == "l",
                              int(size) if size.isdigit() else 0,
                              int(float(mtime)) if mtime else 0))
        else:
            dirs = set(sh_soft(c, f"{find} -type d -print {cap}").splitlines())
            found = [(p, p in dirs, False, 0, 0)
                     for p in sh_soft(c, f"{find} -print {cap}").splitlines()]
        entries = [{"name": posixpath.basename(p), "path": p, "is_dir": is_dir,
                    "is_link": is_link, "size": size, "mtime": mtime}
                   for p, is_dir, is_link, size, mtime in found if p != path]
        entries.sort(key=lambda e: (not e["is_dir"], e["name"].lower()))
        return {"entries": entries, "truncated": len(entries) >= limit}

    def do_mkdir(mid, path):
        c = get_conn(mid)
        with c["lock"]:
            sftp_of(c).mkdir(path)
        return {"ok": True}

    def do_rename(mid, src, dst):
        c = get_conn(mid)
        try:
            with c["lock"]:
                sftp_of(c).rename(src, dst)
        except OSError:
            sh(c, f"mv -- {shlex.quote(src)} {shlex.quote(dst)}")
        return {"ok": True}

    def do_copy(mid, src, dst):
        c = get_conn(mid)
        sh(c, f"cp -a -- {shlex.quote(src)} {shlex.quote(dst)}")
        return {"ok": True}

    def do_delete(mid, path):
        if path.rstrip("/") in ("", "/"):
            raise Http(400, "refusing to delete /")
        c = get_conn(mid)
        sh(c, f"rm -rf -- {shlex.quote(path)}")
        return {"ok": True}

    def do_localize(mid, path):
        c = get_conn(mid)
        with c["tlock"]:
            st = tsftp(c).stat(path)
        size = st.st_size or 0
        mtime = int(st.st_mtime or 0)
        base = posixpath.basename(path) or "file"
        key = hashlib.sha1(f"{mid}:{path}:{mtime}:{size}".encode()).hexdigest()[:12]
        dest_dir = os.path.join(PREVIEW_DIR, mid, key)
        dest = os.path.join(dest_dir, base)
        if not (os.path.isfile(dest) and os.path.getsize(dest) == size):
            os.makedirs(dest_dir, exist_ok=True)
            with c["tlock"]:
                f = tsftp(c).open(path, "rb")
                f.prefetch()
                with f, open(dest, "wb") as out:
                    while True:
                        chunk = f.read(256 * 1024)
                        if not chunk:
                            break
                        out.write(chunk)
        return {"local_path": dest.replace("\\", "/"), "name": base, "size": size}

    def do_upload(mid, directory, name, stream, length):
        c = get_conn(mid)
        dst = posixpath.join(directory, os.path.basename(name or "upload"))
        with c["tlock"]:
            tsftp(c).putfo(_Limited(stream, length), dst, file_size=length)
        return {"ok": True, "path": dst}

    # ---- terminal ----
    def run_term(handler, mid, cols, rows):
        """Bridge an interactive shell to the browser over a hand-rolled WebSocket."""
        key = handler.headers.get("Sec-WebSocket-Key")
        if not key:
            handler.send_error(400, "expected a websocket upgrade")
            return
        handler.wfile.write(
            b"HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\n"
            b"Connection: Upgrade\r\nSec-WebSocket-Accept: "
            + _ws_accept(key).encode() + b"\r\n\r\n")
        handler.wfile.flush()
        handler.close_connection = True
        wlock = threading.Lock()

        def send(payload, opcode=0x2):
            """Shell output goes out as BINARY frames. A text frame has to be
            valid UTF-8 on its own, but chan.recv() cuts at an arbitrary byte and
            can split a multibyte character in half — the browser drops the whole
            connection over that (1007 per the RFC; Chrome just aborts it), and
            the terminal dies mid-session. xterm.js decodes a Uint8Array
            incrementally, so it stitches those halves back together."""
            with wlock:
                handler.wfile.write(_ws_frame(payload, opcode))
                handler.wfile.flush()

        try:
            c = get_conn(mid)
            chan = c["client"].invoke_shell(term="xterm-256color", width=cols, height=rows)
        except Exception as e:
            try:
                send(f"\r\n\x1b[31m{e}\x1b[0m\r\n".encode())
                send(b"", 0x8)
            except OSError:
                pass
            return
        handler.connection.settimeout(None)      # a terminal may idle indefinitely
        open_terms[0] += 1
        last_hit[0] = time.time()
        alive = [True]

        def pump():
            while alive[0]:
                try:
                    data = chan.recv(65536)
                except Exception:
                    data = b""
                if not data:
                    break
                last_hit[0] = time.time()
                try:
                    send(data)
                except OSError:
                    break
            alive[0] = False
            try:
                send(b"", 0x8)
            except OSError:
                pass
            try:
                handler.connection.shutdown(socket.SHUT_RD)   # unblock the reader
            except OSError:
                pass

        reader = threading.Thread(target=pump, daemon=True)
        reader.start()
        try:
            while alive[0]:
                opcode, payload = _ws_read(handler.rfile)
                if opcode is None or opcode == 0x8:
                    break
                last_hit[0] = time.time()
                if opcode == 0x9:
                    send(payload, 0xA)
                    continue
                if opcode not in (0x1, 0x2):
                    continue
                msg = json.loads(payload.decode("utf-8", "replace"))
                if "data" in msg:
                    chan.send(msg["data"].encode("utf-8"))
                elif "resize" in msg:
                    chan.resize_pty(width=int(msg["resize"][0]), height=int(msg["resize"][1]))
        except (OSError, ValueError):
            pass
        finally:
            alive[0] = False
            open_terms[0] -= 1
            try:
                chan.close()
            except Exception:
                pass

    # ---- http ----
    def one(q, key, default=""):
        return q.get(key, [default])[0]

    class H(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"      # keep-alive: no TCP setup per listing
        timeout = 300

        def log_message(self, *a):
            pass

        def _cors(self):
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Cache-Control", "no-store")

        def do_OPTIONS(self):
            self.send_response(204)
            self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
            self.send_header("Access-Control-Allow-Headers", "Content-Type")
            self.send_header("Access-Control-Max-Age", "600")
            self.send_header("Content-Length", "0")
            self._cors()
            self.end_headers()

        def _send(self, code, obj):
            body = json.dumps(obj, default=str).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self._cors()
            self.end_headers()
            try:
                self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError):
                pass

        def _guard(self, u, q):
            if u.path == "/ping":
                return True
            if one(q, "t") != TOKEN:
                self._send(403, {"detail": "forbidden"})
                return False
            return True

        def _body(self):
            n = int(self.headers.get("Content-Length") or 0)
            return json.loads(self.rfile.read(n) or b"{}") if n else {}

        def _dispatch(self, fn):
            try:
                self._send(200, fn())
            except Http as e:
                self._send(e.code, {"detail": e.detail})
            except Exception as e:
                self._send(500, {"detail": str(e) or type(e).__name__})

        def do_GET(self):
            last_hit[0] = time.time()
            u = urlparse(self.path)
            q = parse_qs(u.query)
            if not self._guard(u, q):
                return
            if u.path == "/term":
                run_term(self, one(q, "id"), int(one(q, "cols", "120")),
                         int(one(q, "rows", "32")))
                return
            if u.path == "/download":
                self._download(one(q, "id"), one(q, "path"))
                return
            if u.path == "/ping":
                self._send(200, {"ok": True, "version": VERSION})
            elif u.path == "/quit":
                self._send(200, {"ok": True})
                threading.Thread(target=srv.shutdown, daemon=True).start()
            elif u.path == "/machines":
                self._dispatch(do_machines)
            elif u.path == "/known":
                self._dispatch(lambda: do_known(int(one(q, "limit", "25"))))
            elif u.path == "/ls":
                self._dispatch(lambda: do_ls(one(q, "id"), one(q, "path")))
            elif u.path == "/search":
                self._dispatch(lambda: do_search(one(q, "id"), one(q, "path"),
                                                 one(q, "q"), int(one(q, "limit", "400"))))
            elif u.path == "/localize":
                self._dispatch(lambda: do_localize(one(q, "id"), one(q, "path")))
            else:
                self._send(404, {"detail": "not found"})

        def do_POST(self):
            last_hit[0] = time.time()
            u = urlparse(self.path)
            q = parse_qs(u.query)
            if not self._guard(u, q):
                return
            if u.path == "/upload":
                n = int(self.headers.get("Content-Length") or 0)
                self._dispatch(lambda: do_upload(one(q, "id"), one(q, "dir"),
                                                 one(q, "name"), self.rfile, n))
                return
            parts = [p for p in u.path.split("/") if p]
            if parts[:1] == ["machines"] and len(parts) == 3:
                mid, verb = parts[1], parts[2]
                if verb == "update":
                    self._dispatch(lambda: do_update(mid, self._body()))
                elif verb == "remove":
                    self._dispatch(lambda: do_remove(mid))
                elif verb == "hide":
                    self._dispatch(lambda: do_hide(mid))
                elif verb == "unhide":
                    self._dispatch(lambda: do_unhide(mid))
                else:
                    self._send(404, {"detail": "not found"})
                return
            if u.path == "/machines/add":
                self._dispatch(lambda: do_add(self._body()))
            elif u.path == "/connect":
                self._dispatch(lambda: do_connect(one(q, "id")))
            elif u.path == "/probe":
                self._dispatch(lambda: do_probe(one(q, "id")))
            elif u.path == "/reach":
                self._dispatch(lambda: do_reach(one(q, "id")))
            elif u.path == "/disconnect":
                self._dispatch(lambda: (drop_conn(one(q, "id")), {"ok": True})[1])
            elif u.path == "/mkdir":
                self._dispatch(lambda: do_mkdir(one(q, "id"), one(q, "path")))
            elif u.path == "/rename":
                self._dispatch(lambda: do_rename(one(q, "id"), one(q, "src"), one(q, "dst")))
            elif u.path == "/copy":
                self._dispatch(lambda: do_copy(one(q, "id"), one(q, "src"), one(q, "dst")))
            elif u.path == "/delete":
                self._dispatch(lambda: do_delete(one(q, "id"), one(q, "path")))
            else:
                self._send(404, {"detail": "not found"})

        def _download(self, mid, path):
            try:
                c = get_conn(mid)
                with c["tlock"]:
                    size = tsftp(c).stat(path).st_size or 0
                    f = tsftp(c).open(path, "rb")
                    f.prefetch()
            except Http as e:
                self._send(e.code, {"detail": e.detail})
                return
            except Exception as e:
                self._send(500, {"detail": str(e) or type(e).__name__})
                return
            name = posixpath.basename(path)
            self.send_response(200)
            self.send_header("Content-Type", "application/octet-stream")
            self.send_header("Content-Length", str(size))
            self.send_header("Content-Disposition", f'attachment; filename="{name}"')
            self._cors()
            self.end_headers()
            try:
                with c["tlock"], f:
                    while True:
                        chunk = f.read(256 * 1024)
                        if not chunk:
                            break
                        self.wfile.write(chunk)
            except (BrokenPipeError, ConnectionResetError):
                self.close_connection = True

    srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
    srv.daemon_threads = True
    port = srv.server_address[1]
    os.makedirs(os.path.dirname(STATE), exist_ok=True)
    with open(STATE, "w", encoding="utf-8") as f:
        json.dump({"port": port, "token": TOKEN, "version": VERSION,
                   "pid": os.getpid()}, f)

    def idle_watch():
        while True:
            time.sleep(60)
            if open_terms[0] == 0 and time.time() - last_hit[0] > IDLE_EXIT_S:
                srv.shutdown()
                return
    threading.Thread(target=idle_watch, daemon=True).start()
    print(f"vps stdlib daemon on 127.0.0.1:{port} (v{VERSION})", flush=True)
    srv.serve_forever()


class _Limited:
    """Exactly `length` bytes off a socket stream — paramiko's putfo would otherwise
    read past the request body into the next keep-alive request."""

    def __init__(self, stream, length):
        self.stream = stream
        self.left = length

    def read(self, n=-1):
        if self.left <= 0:
            return b""
        want = self.left if n is None or n < 0 else min(n, self.left)
        data = self.stream.read(want)
        self.left -= len(data)
        return data


if __name__ == "__main__":
    if "--serve" in sys.argv:
        _serve()
    else:
        print(json.dumps(main(), indent=2))
