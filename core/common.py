"""
core/common.py  —  Shared foundation
──────────────────────────────────────
Constants, colour codes, logging helpers, tool resolution,
subprocess wrappers, file-ownership helper, and walk_real().

Every other module does:
    from core.common import *
"""

import os
import sys
import shutil
import struct
import subprocess
import platform
import datetime
import tempfile
import re
import glob

# ─── version / paths ──────────────────────────────────────────────────────────
VERSION    = "4.0"
SCRIPT_DIR = os.path.dirname(os.path.realpath(
    sys.argv[0] if os.path.isfile(sys.argv[0]) else __file__
))
TOOLS_DIR  = os.path.join(SCRIPT_DIR, "tools")
WORK_DIR   = os.path.join(SCRIPT_DIR, "workspace")
LOG_FILE   = os.path.join(SCRIPT_DIR, "rom_tool.log")

# ─── ubuntu check ─────────────────────────────────────────────────────────────
def check_ubuntu():
    if not os.path.exists("/etc/os-release"):
        sys.exit("ERROR: Cannot detect OS — /etc/os-release missing.")
    info = {}
    with open("/etc/os-release") as f:
        for line in f:
            line = line.strip()
            if "=" in line:
                k, v = line.split("=", 1)
                info[k] = v.strip('"')
    if info.get("ID", "") != "ubuntu":
        sys.exit(f"ERROR: Ubuntu required (detected: {info.get('ID','unknown')})")
    ver = info.get("VERSION_ID", "0")
    try:
        major = int(ver.split(".")[0])
    except ValueError:
        major = 0
    if major < 24:
        sys.exit(f"ERROR: Ubuntu 24+ required (detected: {ver})")

# ─── real user detection ──────────────────────────────────────────────────────
def _detect_real_user():
    import pwd as _pwd
    u = os.environ.get("SUDO_USER", "").strip()
    if u and u != "root":
        try:
            pw = _pwd.getpwnam(u)
            return u, pw.pw_uid, pw.pw_gid
        except Exception:
            pass
    try:
        u = subprocess.check_output(["logname"], stderr=subprocess.DEVNULL,
                                    text=True).strip()
        if u and u != "root":
            pw = _pwd.getpwnam(u)
            return u, pw.pw_uid, pw.pw_gid
    except Exception:
        pass
    try:
        out = subprocess.check_output(["who", "-m"], stderr=subprocess.DEVNULL,
                                      text=True).strip()
        u = out.split()[0] if out.split() else ""
        if u and u != "root":
            pw = _pwd.getpwnam(u)
            return u, pw.pw_uid, pw.pw_gid
    except Exception:
        pass
    try:
        for pid_dir in sorted(os.listdir("/proc")):
            if not pid_dir.isdigit():
                continue
            try:
                with open(f"/proc/{pid_dir}/environ", "rb") as f:
                    env = dict(
                        e.split(b"=", 1) for e in f.read().split(b"\x00")
                        if b"=" in e
                    )
                u = env.get(b"USER", b"").decode(errors="replace").strip()
                if u and u != "root":
                    pw = _pwd.getpwnam(u)
                    return u, pw.pw_uid, pw.pw_gid
            except Exception:
                continue
    except Exception:
        pass
    uid = os.getuid()
    gid = os.getgid()
    try:
        import pwd as _pwd2
        u = _pwd2.getpwuid(uid).pw_name
    except Exception:
        u = str(uid)
    return u, uid, gid

REAL_USER, REAL_UID, REAL_GID = _detect_real_user()

# ─── colours ──────────────────────────────────────────────────────────────────
R    = "\033[0;31m"
G    = "\033[0;32m"
Y    = "\033[1;33m"
C    = "\033[0;36m"
W    = "\033[1;37m"
M    = "\033[0;35m"
DIM  = "\033[2m"
N    = "\033[0m"
BOLD = "\033[1m"

# ─── logging ──────────────────────────────────────────────────────────────────
def _logwrite(line):
    try:
        with open(LOG_FILE, "a") as f:
            f.write(line + "\n")
    except Exception:
        pass

def log(msg):
    ts = datetime.datetime.now().strftime("%H:%M:%S")
    line = f"{ts} [INFO]  {msg}"
    print(f"{DIM}{line}{N}")
    _logwrite(line)

def ok(msg):
    ts = datetime.datetime.now().strftime("%H:%M:%S")
    line = f"{ts} [OK]    {msg}"
    print(f"{G}{line}{N}")
    _logwrite(line)

def warn(msg):
    ts = datetime.datetime.now().strftime("%H:%M:%S")
    line = f"{ts} [WARN]  {msg}"
    print(f"{Y}{line}{N}")
    _logwrite(line)

def err(msg):
    ts = datetime.datetime.now().strftime("%H:%M:%S")
    line = f"{ts} [ERROR] {msg}"
    print(f"{R}{line}{N}", file=sys.stderr)
    _logwrite(line)

def header(msg):
    """Bold section separator — no timestamp."""
    print(f"\n  {W}━━━ {msg} ━━━{N}")

def cprint(colour, msg):
    print(f"{colour}{msg}{N}")

# ─── tool resolution ──────────────────────────────────────────────────────────
def arch():
    m = platform.machine()
    if m == "x86_64":  return "x86_64"
    if m == "aarch64": return "aarch64"
    sys.exit(f"Unsupported architecture: {m}")

def tool(name):
    """Return absolute path to bundled or system tool; None if not found."""
    bundled = os.path.join(SCRIPT_DIR, "binaries", "bin", "Linux", arch(), name)
    if os.path.isfile(bundled):
        os.chmod(bundled, 0o755)
        return bundled
    system = shutil.which(name)
    if system:
        return system
    return None

def require_tool(name):
    t = tool(name)
    if not t:
        sys.exit(f"Required tool not found: {name}\nRun option [3] Setup first.")
    return t

# ─── subprocess helpers ───────────────────────────────────────────────────────
def run(cmd, check=True, capture=False, stdin=None):
    """Run a command, logging it. Returns CompletedProcess."""
    _logwrite(f"RUN: {' '.join(str(c) for c in cmd)}")
    kwargs = dict(
        stdin  = stdin,
        stdout = subprocess.PIPE if capture else None,
        stderr = subprocess.PIPE if capture else None,
    )
    result = subprocess.run([str(c) for c in cmd], **kwargs)
    if result.returncode != 0 and check:
        out = (result.stderr or b"").decode(errors="replace").strip()
        raise RuntimeError(
            f"Command failed (rc={result.returncode}): "
            f"{' '.join(str(c) for c in cmd)}\n{out}"
        )
    return result

def sudo_run(cmd, check=True):
    return run(["sudo"] + [str(c) for c in cmd], check=check)

# ─── ownership ────────────────────────────────────────────────────────────────
def own(path):
    """Recursively give path to the real (non-root) user."""
    if not os.path.exists(path):
        return
    log(f"Setting ownership → {REAL_USER} ({REAL_UID}:{REAL_GID}) on {path}")
    try:
        r = subprocess.run(
            ["sudo", "chown", "-R", f"{REAL_UID}:{REAL_GID}", path],
            capture_output=True, text=True
        )
        if r.returncode != 0:
            warn(f"chown -R failed: {r.stderr.strip()}")
            subprocess.run(
                f"sudo find {path!r} -exec chown {REAL_UID}:{REAL_GID} {{}} \\;",
                shell=True, stderr=subprocess.DEVNULL
            )
        subprocess.run(["sudo", "chmod", "-R", "u+rwX", path], capture_output=True)
        stat = os.stat(path)
        if stat.st_uid != REAL_UID:
            warn(f"Ownership verification FAILED — still uid={stat.st_uid}")
            warn("Try:  sudo chown -R $(logname) " + path)
        else:
            log(f"Ownership OK — {path} now owned by {REAL_USER}")
    except Exception as e:
        warn(f"own() exception: {e}")
        warn("Run manually:  sudo chown -R $(logname):$(id -gn $(logname)) " + path)

# ─── filesystem helpers ───────────────────────────────────────────────────────
def walk_real(top, skip_abs=None):
    """
    Like os.walk but symlinks-to-directories are yielded as FILES, not dirs.

    os.walk silently drops symlink-dirs when followlinks=False.
    This wrapper moves them into the 'files' list so every path is visited.
    """
    skip_abs = skip_abs or set()
    for root, dirs, files in os.walk(top, followlinks=False):
        real_dirs    = []
        symlink_dirs = []
        for d in dirs:
            full = os.path.join(root, d)
            if os.path.realpath(full) in skip_abs:
                continue
            if os.path.islink(full):
                symlink_dirs.append(d)
            else:
                real_dirs.append(d)
        dirs[:] = sorted(real_dirs)
        yield root, dirs, sorted(files) + sorted(symlink_dirs)

def makedirs(path):
    os.makedirs(path, exist_ok=True)
    own(path)
