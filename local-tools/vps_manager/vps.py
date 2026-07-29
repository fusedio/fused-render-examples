# /// script
# dependencies = ["paramiko>=3", "fastapi", "uvicorn"]
# ///
"""SSH connection daemon for the VPS Manager example.

Each fused-render runPython call is a fresh subprocess, so SSH sessions can't
live there. This module is both:

  1. a runPython entrypoint `main(action="ensure")` — starts (or reuses) a
     long-lived localhost daemon and returns its port; and
  2. the daemon (run as `python vps.py --serve`) — holds live paramiko
     connections per machine and serves HTTP + a terminal WebSocket.

Endpoints (CORS *):
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
  POST /connect?id=                -> {"ok", "home"}  (400 with reason if not)
  POST /probe?id=                  -> {"ok", "ms", "shell", "sftp", "detail"}
  POST /reach?id=                  -> {"ok", "ms", "banner"}  (TCP only, no login)
  POST /disconnect?id=             -> {"ok"}
  GET  /ls?id=&path=               -> {path, parent, entries: [{name, is_dir,
                                       is_link, size, mtime}]}
  GET  /search?id=&path=&q=        -> {entries: [{name, path, is_dir, size,
                                       mtime}], truncated}  (recursive find)
  POST /mkdir?id=&path=            -> {"ok"}
  POST /rename?id=&src=&dst=       -> {"ok"}   (also move)
  POST /copy?id=&src=&dst=         -> {"ok"}
  POST /delete?id=&path=           -> {"ok"}
  GET  /download?id=&path=         -> file bytes
  GET  /localize?id=&path=         -> {local_path, name, size}  (cache for preview)
  POST /upload?id=&dir=            -> multipart file -> {"ok"}
  WS   /term?id=&cols=&rows=       -> in: {"data"} | {"resize": [c, r]},
                                       out: raw shell output text

The machine list is ~/.ssh/config — plus /etc/ssh/ssh_config, or %ProgramData%/ssh
on Windows — read live, ids `cfg:<alias>`, Include directives expanded, first
setting wins as in ssh; plus anything added by hand in machines.json beside this
file (passwords included — prefer key_path). A hand-added machine on the same
user@host:port as a config alias supersedes it. /known adds the hosts that only
~/.ssh/known_hosts remembers (ids `kh:<host>:<port>`, username guessed) so a box
rented for an afternoon can be picked up again. Idle shutdown after 30 min with
no requests and no open terminals. The state file embeds this module's mtime, so
editing it respawns a fresh daemon on the next ensure().
"""
import glob
import hashlib
import json
import os
import posixpath
import sys
import threading
import time

STATE = os.path.expanduser("~/.cache/fused-render-vps-v1/daemon.json")
PREVIEW_DIR = os.path.expanduser("~/.cache/fused-render-vps-v1/preview")
HERE = os.path.dirname(os.path.abspath(__file__)) if "__file__" in globals() else os.path.abspath(sys.path[0])
MACHINES = os.path.join(HERE, "machines.json")
IDLE_EXIT_S = 30 * 60

# OpenSSH keeps per-user files in ~/.ssh on macOS, Linux and Windows alike
# (expanduser falls back to %USERPROFILE% there). Only the system-wide directory
# moves: /etc/ssh on unix, %ProgramData%\ssh for Windows' OpenSSH port.
SSH_DIR = os.path.join(os.path.expanduser("~"), ".ssh")
SSH_CONFIG = os.path.join(SSH_DIR, "config")
KNOWN_HOSTS = os.path.join(SSH_DIR, "known_hosts")
SYSTEM_SSH_DIR = (os.path.join(os.environ.get("ProgramData", r"C:\ProgramData"), "ssh")
                  if os.name == "nt" else "/etc/ssh")
SYSTEM_CONFIG = os.path.join(SYSTEM_SSH_DIR, "ssh_config")
SYSTEM_KNOWN_HOSTS = os.path.join(SYSTEM_SSH_DIR, "ssh_known_hosts")


def _me():
    return os.path.join(HERE, "vps.py")


def _version():
    try:
        return str(os.path.getmtime(_me())) + "|" + os.path.dirname(sys.executable)
    except OSError:
        return "0"


def _daemon_executable():
    """Prefer the windowless pythonw.exe for the daemon so no console ever flashes
    (Windows Terminal is the default console host on Win11 and CREATE_NO_WINDOW
    doesn't reliably suppress it for a console-subsystem python.exe)."""
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
def _alive(port, version):
    import urllib.request
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/ping", timeout=2) as r:
            d = json.load(r)
        return d.get("ok") and d.get("version") == version
    except Exception:
        return False


def main(action: str = "ensure"):
    """runPython entrypoint: make sure the daemon is running, return {port}."""
    import subprocess
    try:
        import paramiko  # noqa: F401
    except ImportError:
        return {"error": "paramiko is not installed — run: uv pip install paramiko "
                         f"--python {sys.executable}"}
    version = _version()
    try:
        with open(STATE) as f:
            st = json.load(f)
        if _alive(st.get("port"), version):
            return {"port": st["port"], "reused": True, "version": version}
        try:
            import urllib.request
            urllib.request.urlopen(
                f"http://127.0.0.1:{st.get('port')}/quit", timeout=1).read()
        except Exception:
            pass
    except (OSError, ValueError):
        pass

    os.makedirs(os.path.dirname(STATE), exist_ok=True)
    log = os.path.join(os.path.dirname(STATE), "daemon.log")
    kwargs = {}
    if os.name == "nt":
        kwargs["creationflags"] = 0x00000008 | 0x00000200 | 0x08000000  # DETACHED_PROCESS | NEW_PROCESS_GROUP | CREATE_NO_WINDOW
    else:
        kwargs["start_new_session"] = True
    with open(log, "ab") as lf:
        subprocess.Popen([_daemon_executable(), _me(), "--serve"],
                         stdout=lf, stderr=lf, cwd=HERE, **kwargs)
    for _ in range(200):
        time.sleep(0.05)
        try:
            with open(STATE) as f:
                st = json.load(f)
            if st.get("version") == version and _alive(st.get("port"), version):
                return {"port": st["port"], "reused": False, "version": version}
        except (OSError, ValueError):
            continue
    return {"error": f"daemon did not start — see {log}"}


# ================================================================ daemon
def _serve():
    import getpass
    import shlex
    import socket
    import stat as statmod
    import uuid

    import paramiko
    import uvicorn
    from fastapi import FastAPI, HTTPException, Request, UploadFile, WebSocket
    from fastapi.middleware.cors import CORSMiddleware
    from fastapi.responses import StreamingResponse

    VERSION = _version()
    last_hit = [time.time()]
    open_terms = [0]

    app = FastAPI()
    app.add_middleware(CORSMiddleware, allow_origins=["*"],
                       allow_methods=["*"], allow_headers=["*"])

    @app.middleware("http")
    async def touch(request: Request, call_next):
        last_hit[0] = time.time()
        return await call_next(request)

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
        raise HTTPException(404, f"unknown machine {mid}")

    def public(m):
        return {k: v for k, v in m.items() if k != "password"}

    # ---- live connections ----
    conns = {}            # id -> {"client", "jump", "sftp", "home", "key_used", "lock"}
    conns_lock = threading.Lock()

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
            raise HTTPException(400, "ProxyCommand hosts aren't supported — "
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
                    raise HTTPException(400, "ProxyJump chain is too long")
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

    def get_conn(mid):
        m = get_machine(mid)
        if is_connected(mid):
            return conns[mid]
        drop_conn(mid)
        client, jump, key_used = open_client(m)
        try:
            sftp = client.open_sftp()
            home = sftp.normalize(".")
        except Exception:
            sftp, home = None, "/"     # git-only hosts authenticate but refuse SFTP
        c = {"client": client, "jump": jump, "sftp": sftp, "home": home,
             "key_used": key_used, "username": m["username"], "lock": threading.Lock(),
             "tsftp": None, "tlock": threading.Lock()}
        with conns_lock:
            conns[mid] = c
        return c

    def sftp_of(c):
        if c["sftp"] is None:
            raise HTTPException(400, "this host allows SSH but not SFTP, so there are "
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

    def sh(conn, cmd):
        _, out, err = conn["client"].exec_command(cmd, timeout=60)
        rc = out.channel.recv_exit_status()
        if rc != 0:
            msg = err.read().decode("utf-8", "replace").strip()
            raise HTTPException(400, msg or f"command failed (exit {rc})")
        return out.read().decode("utf-8", "replace")

    def sh_soft(conn, cmd):
        _, out, _ = conn["client"].exec_command(cmd, timeout=60)
        data = out.read().decode("utf-8", "replace")
        out.channel.recv_exit_status()
        return data

    # ---- endpoints ----
    @app.get("/ping")
    def ping():
        return {"ok": True, "version": VERSION}

    @app.get("/quit")
    def quit_():
        threading.Timer(0.2, os._exit, (0,)).start()
        return {"ok": True}

    @app.get("/machines")
    def machines():
        pool, hidden, err = all_machines()
        return {"machines": [dict(public(m), connected=is_connected(m["id"]))
                             for m in pool],
                "hidden": [public(m) for m in hidden],
                "config_path": SSH_CONFIG if os.path.isfile(SSH_CONFIG) else "",
                "config_error": err}

    @app.post("/machines/add")
    def machines_add(body: dict):
        m = {"id": uuid.uuid4().hex[:8],
             "name": (body.get("name") or body.get("host") or "").strip(),
             "host": (body.get("host") or "").strip(),
             "port": int(body.get("port") or 22),
             "username": (body.get("username") or "").strip(),
             "key_path": (body.get("key_path") or "").strip(),
             "password": body.get("password") or ""}
        if not m["host"] or not m["username"]:
            raise HTTPException(400, "host and username are required")
        with reg_lock:
            ms = load_machines()
            ms.append(m)
            save_machines(ms)
        return {"machine": public(m)}

    @app.get("/known")
    def known(limit: int = 25):
        pool, hidden, _ = all_machines()
        taken = set()
        for m in pool + hidden:      # known_hosts keys entries by alias or hostname
            taken.add((m["host"].lower(), int(m.get("port") or 22)))
            taken.add((m["name"].lower(), int(m.get("port") or 22)))
        hosts, total, hashed = known_hosts_machines(limit, taken)
        paths = [p for p in known_hosts_files(_parse_config()[0]) if os.path.isfile(p)]
        return {"hosts": hosts, "total": total, "hashed": hashed,
                "path": "\n".join(paths)}

    @app.post("/reach")
    def reach(id: str):
        """Is anything listening? TCP connect and read the SSH banner, no login."""
        m = get_machine(id)
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

    def cfg_alias(mid):
        if not mid.startswith("cfg:"):
            raise HTTPException(400, "not an ssh-config host")
        return mid[4:]

    def reject_derived(mid, verb):
        if mid.startswith("cfg:"):
            raise HTTPException(400, f"{mid[4:]} comes from {SSH_CONFIG} — edit that "
                                     f"file to {verb} it, or hide it from this list")
        if mid.startswith("kh:"):
            raise HTTPException(400, "this host is only remembered in known_hosts — "
                                     "save it here first")

    @app.post("/machines/{mid}/hide")
    def machines_hide(mid: str):
        alias = cfg_alias(mid)
        drop_conn(mid)
        with reg_lock:
            reg = load_registry()
            if alias not in reg["hidden"]:
                reg["hidden"].append(alias)
                save_registry(reg)
        return {"ok": True}

    @app.post("/machines/{mid}/unhide")
    def machines_unhide(mid: str):
        alias = cfg_alias(mid)
        with reg_lock:
            reg = load_registry()
            reg["hidden"] = [a for a in reg["hidden"] if a != alias]
            save_registry(reg)
        return {"ok": True}

    @app.post("/machines/{mid}/update")
    def machines_update(mid: str, body: dict):
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
        raise HTTPException(404, f"unknown machine {mid}")

    @app.post("/machines/{mid}/remove")
    def machines_remove(mid: str):
        reject_derived(mid, "remove")
        drop_conn(mid)
        with reg_lock:
            save_machines([m for m in load_machines() if m["id"] != mid])
        return {"ok": True}

    @app.post("/connect")
    def connect(id: str):
        try:
            c = get_conn(id)
        except HTTPException:
            raise
        except Exception as e:
            drop_conn(id)
            raise HTTPException(400, str(e) or type(e).__name__)
        return {"ok": True, "home": c["home"], "sftp": c["sftp"] is not None}

    @app.post("/probe")
    def probe(id: str):
        """Actually log in and report what answered — never raises for a dead host."""
        t0 = time.time()
        try:
            c = get_conn(id)
        except HTTPException as e:
            return {"ok": False, "error": e.detail}
        except Exception as e:
            drop_conn(id)
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

    @app.post("/disconnect")
    def disconnect(id: str):
        drop_conn(id)
        return {"ok": True}

    @app.get("/ls")
    def ls(id: str, path: str = ""):
        c = get_conn(id)
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

    @app.get("/search")
    def search(id: str, path: str, q: str, limit: int = 400):
        q = q.strip()
        if not q:
            return {"entries": [], "truncated": False}
        c = get_conn(id)
        pat = "*" + q + "*"
        cmd = (f"find {shlex.quote(path)} -maxdepth 8 -iname {shlex.quote(pat)} "
               f"-printf '%y\\t%s\\t%T@\\t%p\\n' 2>/dev/null | head -n {int(limit)}")
        out = sh_soft(c, cmd)
        entries = []
        for line in out.splitlines():
            parts = line.split("\t", 3)
            if len(parts) != 4:
                continue
            typ, size, mtime, p = parts
            if p == path:
                continue
            entries.append({"name": posixpath.basename(p), "path": p,
                            "is_dir": typ == "d", "is_link": typ == "l",
                            "size": int(size) if size.isdigit() else 0,
                            "mtime": int(float(mtime)) if mtime else 0})
        entries.sort(key=lambda e: (not e["is_dir"], e["name"].lower()))
        return {"entries": entries, "truncated": len(entries) >= limit}

    @app.post("/mkdir")
    def mkdir(id: str, path: str):
        c = get_conn(id)
        with c["lock"]:
            sftp_of(c).mkdir(path)
        return {"ok": True}

    @app.post("/rename")
    def rename(id: str, src: str, dst: str):
        c = get_conn(id)
        try:
            with c["lock"]:
                sftp_of(c).rename(src, dst)
        except OSError:
            sh(c, f"mv -- {shlex.quote(src)} {shlex.quote(dst)}")
        return {"ok": True}

    @app.post("/copy")
    def copy(id: str, src: str, dst: str):
        c = get_conn(id)
        sh(c, f"cp -a -- {shlex.quote(src)} {shlex.quote(dst)}")
        return {"ok": True}

    @app.post("/delete")
    def delete(id: str, path: str):
        if path.rstrip("/") in ("", "/"):
            raise HTTPException(400, "refusing to delete /")
        c = get_conn(id)
        sh(c, f"rm -rf -- {shlex.quote(path)}")
        return {"ok": True}

    @app.get("/download")
    def download(id: str, path: str):
        c = get_conn(id)
        with c["tlock"]:
            f = tsftp(c).open(path, "rb")
            f.prefetch()

        def gen():
            with c["tlock"], f:
                while True:
                    chunk = f.read(256 * 1024)
                    if not chunk:
                        break
                    yield chunk
        name = posixpath.basename(path)
        return StreamingResponse(gen(), media_type="application/octet-stream",
                                 headers={"Content-Disposition": f'attachment; filename="{name}"'})

    @app.get("/localize")
    def localize(id: str, path: str):
        c = get_conn(id)
        with c["tlock"]:
            st = tsftp(c).stat(path)
        size = st.st_size or 0
        mtime = int(st.st_mtime or 0)
        base = posixpath.basename(path) or "file"
        key = hashlib.sha1(f"{id}:{path}:{mtime}:{size}".encode()).hexdigest()[:12]
        dest_dir = os.path.join(PREVIEW_DIR, id, key)
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

    @app.post("/upload")
    def upload(id: str, dir: str, file: UploadFile):
        c = get_conn(id)
        dst = posixpath.join(dir, os.path.basename(file.filename or "upload"))
        with c["tlock"]:
            tsftp(c).putfo(file.file, dst)
        return {"ok": True, "path": dst}

    @app.websocket("/term")
    async def term(ws: WebSocket, id: str, cols: int = 120, rows: int = 32):
        import asyncio
        await ws.accept()
        try:
            c = await asyncio.to_thread(get_conn, id)
            chan = c["client"].invoke_shell(term="xterm-256color",
                                            width=cols, height=rows)
        except Exception as e:
            await ws.send_text(f"\r\n\x1b[31m{e}\x1b[0m\r\n")
            await ws.close()
            return
        open_terms[0] += 1
        last_hit[0] = time.time()
        loop = asyncio.get_running_loop()
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
                fut = asyncio.run_coroutine_threadsafe(
                    ws.send_text(data.decode("utf-8", "replace")), loop)
                try:
                    fut.result(timeout=10)
                except Exception:
                    break
            if alive[0]:
                asyncio.run_coroutine_threadsafe(ws.close(), loop)

        reader = threading.Thread(target=pump, daemon=True)
        reader.start()
        try:
            while True:
                msg = json.loads(await ws.receive_text())
                last_hit[0] = time.time()
                if "data" in msg:
                    chan.send(msg["data"].encode("utf-8"))
                elif "resize" in msg:
                    chan.resize_pty(width=int(msg["resize"][0]),
                                    height=int(msg["resize"][1]))
        except Exception:
            pass
        finally:
            alive[0] = False
            open_terms[0] -= 1
            try:
                chan.close()
            except Exception:
                pass

    # ---- boot ----
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()

    os.makedirs(os.path.dirname(STATE), exist_ok=True)
    with open(STATE, "w", encoding="utf-8") as f:
        json.dump({"port": port, "version": VERSION, "pid": os.getpid()}, f)

    def idle_watch():
        while True:
            time.sleep(60)
            if open_terms[0] == 0 and time.time() - last_hit[0] > IDLE_EXIT_S:
                os._exit(0)

    threading.Thread(target=idle_watch, daemon=True).start()
    uvicorn.run(app, host="127.0.0.1", port=port, log_level="warning")


if __name__ == "__main__":
    if "--serve" in sys.argv:
        _serve()
    else:
        print(json.dumps(main(), indent=2))
