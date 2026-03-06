#!/usr/bin/env python3
"""
Android ROM Image Tool — Python Edition
Ubuntu 24+ | ext4 · sparse · super.img · fs_config · SELinux
Supports: system, system_ext, product, vendor, odm
"""

import os
import sys
import shutil
import struct
import subprocess
import platform
import textwrap
import datetime
import glob
import tempfile
import re

# ─── require Ubuntu 24+ ──────────────────────────────────────────────────────
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

# ─── paths ────────────────────────────────────────────────────────────────────
SCRIPT_DIR  = os.path.dirname(os.path.realpath(__file__))
TOOLS_DIR   = os.path.join(SCRIPT_DIR, "tools")
WORK_DIR    = os.path.join(SCRIPT_DIR, "workspace")
LOG_FILE    = os.path.join(SCRIPT_DIR, "rom_tool.log")
VERSION     = "3.0"

# ─── Real user detection ─────────────────────────────────────────────────────
# Works correctly whether run as: python3 rom_tool.py  OR  sudo python3 rom_tool.py
def _detect_real_user():
    import pwd as _pwd
    # 1. SUDO_USER is set when running under sudo — most reliable
    u = os.environ.get("SUDO_USER", "").strip()
    if u and u != "root":
        try:
            pw = _pwd.getpwnam(u)
            return u, pw.pw_uid, pw.pw_gid
        except Exception:
            pass
    # 2. logname gives the login user regardless of sudo
    try:
        u = subprocess.check_output(["logname"], stderr=subprocess.DEVNULL,
                                    text=True).strip()
        if u and u != "root":
            pw = _pwd.getpwnam(u)
            return u, pw.pw_uid, pw.pw_gid
    except Exception:
        pass
    # 3. who am i / who -m — shows the user at the physical terminal
    try:
        out = subprocess.check_output(["who", "-m"], stderr=subprocess.DEVNULL,
                                      text=True).strip()
        u = out.split()[0] if out.split() else ""
        if u and u != "root":
            pw = _pwd.getpwnam(u)
            return u, pw.pw_uid, pw.pw_gid
    except Exception:
        pass
    # 4. Walk /proc to find the first non-root login session's USER env
    try:
        for pid_dir in sorted(os.listdir("/proc")):
            if not pid_dir.isdigit():
                continue
            env_path = f"/proc/{pid_dir}/environ"
            try:
                with open(env_path, "rb") as f:
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
    # 5. Last resort: current process owner
    uid = os.getuid()
    gid = os.getgid()
    try:
        u = _pwd.getpwuid(uid).pw_name
    except Exception:
        u = str(uid)
    return u, uid, gid

REAL_USER, REAL_UID, REAL_GID = _detect_real_user()

# ─── colours ─────────────────────────────────────────────────────────────────
R  = "\033[0;31m"
G  = "\033[0;32m"
Y  = "\033[1;33m"
C  = "\033[0;36m"
W  = "\033[1;37m"
DIM= "\033[2m"
N  = "\033[0m"
BOLD="\033[1m"

def cprint(colour, msg):
    print(f"{colour}{msg}{N}")

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

def _logwrite(line):
    try:
        with open(LOG_FILE, "a") as f:
            f.write(line + "\n")
    except Exception:
        pass

# ─── tool resolution ──────────────────────────────────────────────────────────
def arch():
    m = platform.machine()
    if m == "x86_64":
        return "x86_64"
    if m == "aarch64":
        return "aarch64"
    sys.exit(f"Unsupported architecture: {m}")

def tool(name):
    """Return absolute path to a bundled or system tool."""
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
        sys.exit(f"Required tool not found: {name}\nRun option [S] Setup first.")
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
        raise RuntimeError(f"Command failed (rc={result.returncode}): {' '.join(str(c) for c in cmd)}\n{out}")
    return result

def sudo_run(cmd, check=True):
    return run(["sudo"] + [str(c) for c in cmd], check=check)

# ─── ownership ────────────────────────────────────────────────────────────────
def own(path):
    """
    Recursively give path to the real (non-root) user.
    Logs clearly on failure — a silent chown failure looks like an empty folder
    in Nautilus/Nemo since the directory appears unreadable to the desktop user.
    """
    if not os.path.exists(path):
        return
    log(f"Setting ownership → {REAL_USER} ({REAL_UID}:{REAL_GID}) on {path}")
    try:
        r = subprocess.run(
            ["sudo", "chown", "-R", f"{REAL_UID}:{REAL_GID}", path],
            capture_output=True, text=True
        )
        if r.returncode != 0:
            warn(f"chown -R failed (rc={r.returncode}): {r.stderr.strip()}")
            warn("Trying per-file chown fallback …")
            # Per-file fallback using find — handles immutable/special files
            subprocess.run(
                f"sudo find {path!r} -exec chown {REAL_UID}:{REAL_GID} {{}} \\;",
                shell=True, stderr=subprocess.DEVNULL
            )
        subprocess.run(
            ["sudo", "chmod", "-R", "u+rwX", path],
            capture_output=True
        )
        # Verify: check the top-level dir is now readable by the real user
        stat = os.stat(path)
        if stat.st_uid != REAL_UID:
            warn(f"Ownership verification FAILED — {path} still owned by uid={stat.st_uid}, not {REAL_UID}")
            warn("Try running:  sudo chown -R $(logname) " + path)
        else:
            log(f"Ownership OK — {path} now owned by {REAL_USER}")
    except Exception as e:
        warn(f"own() exception: {e}")
        warn("Run manually:  sudo chown -R $(logname):$(id -gn $(logname)) " + path)


def walk_real(top, skip_abs=None):
    """
    Like os.walk but symlinks-to-directories are yielded as FILES, not dirs.

    The standard os.walk (followlinks=False) uses entry.is_dir() which follows
    symlinks by default, so a symlink like /d → /sys/kernel/debug lands in
    'dirs'. os.walk then skips recursing into it (followlinks=False) but it
    never shows up in 'files' either — it simply vanishes from the iteration.

    This wrapper detects those symlink-dirs and moves them to 'files' so every
    path in the tree is always visited exactly once regardless of whether it is
    a real directory, a regular file, or a symlink.

    Args:
        top      : root directory to walk
        skip_abs : set of absolute realpath strings to skip (e.g. META dir)
    """
    skip_abs = skip_abs or set()
    for root, dirs, files in os.walk(top, followlinks=False):
        # Partition dirs into real dirs vs symlinks-to-dirs
        real_dirs  = []
        symlink_dirs = []
        for d in dirs:
            full = os.path.join(root, d)
            if os.path.realpath(full) in skip_abs:
                continue   # skip META etc.
            if os.path.islink(full):
                symlink_dirs.append(d)   # treat as a file entry
            else:
                real_dirs.append(d)
        dirs[:] = sorted(real_dirs)
        yield root, dirs, sorted(files) + sorted(symlink_dirs)

def makedirs(path):
    os.makedirs(path, exist_ok=True)
    own(path)

# ─── image format detection ───────────────────────────────────────────────────
def detect_format(img):
    """Return 'sparse', 'ext4', 'super', or 'unknown'."""
    try:
        with open(img, "rb") as f:
            header = f.read(4096)
    except Exception:
        return "unknown"

    # Android sparse magic: 0xED26FF3A  (little-endian)
    if len(header) >= 4 and struct.unpack_from("<I", header, 0)[0] == 0xED26FF3A:
        return "sparse"

    # ext4 superblock magic at offset 1080: 0xEF53
    if len(header) >= 1082:
        sb_magic = struct.unpack_from("<H", header, 1080)[0]
        if sb_magic == 0xEF53:
            return "ext4"

    # LP (super) metadata magic at offset 4096: 0x4D0CC467
    if len(header) >= 4096 + 4:
        lp_magic = struct.unpack_from("<I", header, 4096)[0]
        if lp_magic == 0x4D0CC467:
            return "super"

    # Try reading ext4 superblock from a raw image (may start at block 0)
    try:
        with open(img, "rb") as f:
            f.seek(1024)
            sb = f.read(2)
        if len(sb) == 2 and struct.unpack("<H", sb)[0] == 0xEF53:
            return "ext4"
    except Exception:
        pass

    return "unknown"

# ─── sparse ↔ raw ─────────────────────────────────────────────────────────────
def to_raw(src, dst):
    fmt = detect_format(src)
    if fmt == "sparse":
        log(f"Converting sparse → raw …")
        run([require_tool("simg2img"), src, dst])
    else:
        log("Already raw — copying …")
        shutil.copy2(src, dst)

def to_sparse(src, dst):
    log("Converting raw → sparse …")
    run([require_tool("img2simg"), src, dst])

# ─── metadata helpers ─────────────────────────────────────────────────────────
KNOWN_MOUNT_POINTS = {
    "system":     "/",
    "system_ext": "/system_ext",
    "product":    "/product",
    "vendor":     "/vendor",
    "odm":        "/odm",
    "vendor_dlkm":"/vendor_dlkm",
    "odm_dlkm":   "/odm_dlkm",
    "system_dlkm":"/system_dlkm",
}

def save_image_meta(raw_img, meta_dir, part_name):
    makedirs(meta_dir)
    dump_path = os.path.join(meta_dir, "dumpe2fs.txt")

    # Run dumpe2fs
    try:
        r = run(["dumpe2fs", "-h", raw_img], capture=True, check=False)
        dump = r.stdout.decode(errors="replace")
        with open(dump_path, "w") as f:
            f.write(dump)
    except Exception:
        dump = ""

    def extract(pattern):
        m = re.search(pattern, dump, re.MULTILINE)
        return m.group(1).strip() if m else ""

    block_size   = extract(r"^Block size:\s+(\S+)")
    block_count  = extract(r"^Block count:\s+(\S+)")
    inode_count  = extract(r"^Inode count:\s+(\S+)")
    inode_size   = extract(r"^Inode size:\s+(\S+)")
    mount_point  = extract(r"^Last mounted on:\s+(\S+)")
    label        = extract(r"^Filesystem volume name:\s+(\S+)")

    # Fallback mount point
    if not mount_point or mount_point.startswith("<"):
        mount_point = KNOWN_MOUNT_POINTS.get(part_name, f"/{part_name}")

    img_size = os.path.getsize(raw_img)

    info = {
        "partition_name":       part_name,
        "mount_point":          mount_point,
        "block_size":           block_size or "4096",
        "block_count":          block_count or "0",
        "inode_count":          inode_count or "0",
        "inode_size":           inode_size or "256",
        "label":                label or "",
        "original_size":        str(img_size),
        "original_was_sparse":  "0",
    }

    info_path = os.path.join(meta_dir, "image_info.txt")
    with open(info_path, "w") as f:
        for k, v in info.items():
            f.write(f"{k}={v}\n")

    log(f"Saved metadata → {info_path}")
    return info

def load_meta(meta_dir):
    info_path = os.path.join(meta_dir, "image_info.txt")
    if not os.path.isfile(info_path):
        raise FileNotFoundError(f"META not found: {info_path}")
    info = {}
    with open(info_path) as f:
        for line in f:
            line = line.strip()
            if "=" in line:
                k, v = line.split("=", 1)
                info[k] = v
    return info

def set_meta(meta_dir, key, value):
    info_path = os.path.join(meta_dir, "image_info.txt")
    lines = []
    found = False
    with open(info_path) as f:
        for line in f:
            if line.startswith(f"{key}="):
                lines.append(f"{key}={value}\n")
                found = True
            else:
                lines.append(line)
    if not found:
        lines.append(f"{key}={value}\n")
    with open(info_path, "w") as f:
        f.writelines(lines)

# ─── fs_config extraction (from mounted image) ────────────────────────────────
def extract_fs_config(mount_dir, meta_dir, mount_point):
    cfg_path = os.path.join(meta_dir, "fs_config.txt")
    ctx_path = os.path.join(meta_dir, "file_contexts.txt")

    log("Extracting fs_config and SELinux contexts …")

    cfg_lines = []
    ctx_lines = []

    # Walk every file and directory — walk_real ensures symlinks-to-dirs
    # appear as file entries (not silently skipped)
    all_paths = []
    for root, dirs, files in walk_real(mount_dir):
        all_paths.append(root)
        for fname in files:
            all_paths.append(os.path.join(root, fname))

    part_prefix = mount_point.lstrip("/") or "system"

    for fpath in all_paths:
        rel = fpath[len(mount_dir):]
        if not rel:
            rel = "/"

        try:
            st = os.lstat(fpath)
            uid  = st.st_uid
            gid  = st.st_gid
            mode = oct(st.st_mode)[-4:]   # e.g. "0755"
        except Exception:
            uid, gid, mode = 0, 0, "0644"

        # Capabilities via getfattr
        caps = "0"
        try:
            r = subprocess.run(
                ["getfattr", "-n", "security.capability",
                 "--only-values", "--absolute-names", fpath],
                capture_output=True
            )
            if r.returncode == 0 and r.stdout:
                caps = "0x" + r.stdout.hex()
        except Exception:
            pass

        # Build fs_config path
        if rel == "/":
            cfg_path_entry = part_prefix
        else:
            cfg_path_entry = part_prefix + rel

        cfg_lines.append(f"{cfg_path_entry} {uid} {gid} {mode} {caps}")

        # SELinux context
        ctx = ""
        try:
            r = subprocess.run(
                ["getfattr", "-n", "security.selinux",
                 "--only-values", "--absolute-names", fpath],
                capture_output=True
            )
            if r.returncode == 0:
                ctx = r.stdout.decode(errors="replace").strip().rstrip("\x00")
        except Exception:
            pass

        if not ctx:
            ctx = _default_selinux(cfg_path_entry)

        # SELinux context path — ALWAYS use canonical /<part_name>/<rel> form.
        # Using mount_point ("/" for many system images) produces "//bin/sh" paths
        # which require extra normalisation in every downstream function.
        # Canonical form: "/system/bin/sh", "/vendor/etc/foo", etc.
        if rel == "/":
            ctx_rel = "/" + part_prefix         # e.g. "/system"
        else:
            ctx_rel = "/" + part_prefix + rel   # e.g. "/system/bin/sh"

        # Escape ALL regex metacharacters for file_contexts format.
        # make_ext4fs compiles each path as an ERE — unescaped chars like
        # '+' (lost+found), '.' (foo.conf), '(' / ')' cause lookup failures.
        ctx_rel_esc = _escape_fc_path(ctx_rel)

        # Determine file-type specifier for file_contexts
        # make_ext4fs requires these; omitting causes "invalid file type" warnings
        try:
            st2 = os.lstat(fpath)
            import stat as _stat
            m2 = st2.st_mode
            if _stat.S_ISDIR(m2):   ftype = "-d"
            elif _stat.S_ISLNK(m2): ftype = "-l"
            elif _stat.S_ISFIFO(m2):ftype = "-p"
            elif _stat.S_ISSOCK(m2):ftype = "-s"
            elif _stat.S_ISCHR(m2): ftype = "-c"
            elif _stat.S_ISBLK(m2): ftype = "-b"
            else:                   ftype = "--"   # regular file
        except Exception:
            ftype = "--"

        ctx_lines.append(f"{ctx_rel_esc}    {ftype}    {ctx}")

    with open(cfg_path, "w") as f:
        f.write("\n".join(cfg_lines) + "\n")
    with open(ctx_path, "w") as f:
        f.write("\n".join(ctx_lines) + "\n")

    ok(f"fs_config:      {len(cfg_lines)} entries")
    ok(f"file_contexts:  {len(ctx_lines)} entries")

def _escape_fc_path(path):
    """
    Escape all regex metacharacters in a filesystem path for use in file_contexts.

    make_ext4fs (and the kernel's SELinux labeling) compile each path field in
    file_contexts as a POSIX Extended Regular Expression.  Any metacharacter
    that appears literally in the filename MUST be backslash-escaped so the
    pattern matches the real path rather than being mis-parsed as a quantifier
    or anchor.

    Chars that appear in real Android paths and need escaping:
      .  -> \\.    (nearly every filename with an extension or version dot)
      +  -> \\+    (lost+found -- the classic make_ext4fs killer)
      @  -> (not ERE special, no escaping needed)
      [  -> \\[
      ]  -> \\]
      (  -> \\(    (some vendor HIDL paths)
      )  -> \\)
      {  -> \\{
      }  -> \\}
      ?  -> \\?
      *  -> \\*
      ^  -> \\^
      $  -> \\$
      |  -> \\|

    The leading '/' is preserved unescaped (it is not a regex special char).
    """
    # Escape backslash first to avoid double-escaping
    for ch in r'\.*+?[](){}^$|':
        path = path.replace(ch, '\\' + ch)
    return path

def _default_selinux(cfg_path):
    """Best-guess SELinux label based on path."""
    p = cfg_path.lower()
    if "/vendor/app"       in p: return "u:object_r:vendor_app_file:s0"
    if "/vendor/bin"       in p: return "u:object_r:vendor_file:s0"
    if "/vendor/lib"       in p: return "u:object_r:vendor_file:s0"
    if "/vendor/etc"       in p: return "u:object_r:vendor_configs_file:s0"
    if "vendor/"           in p: return "u:object_r:vendor_file:s0"
    if "/system_ext"       in p: return "u:object_r:system_ext_file:s0"
    if "/product/app"      in p: return "u:object_r:system_app_file:s0"
    if "product/"          in p: return "u:object_r:system_file:s0"
    if "/app/"             in p: return "u:object_r:system_app_file:s0"
    if "/priv-app/"        in p: return "u:object_r:system_app_file:s0"
    if "/framework/"       in p: return "u:object_r:system_file:s0"
    if "/lib/"             in p: return "u:object_r:system_lib_file:s0"
    if "/lib64/"           in p: return "u:object_r:system_lib_file:s0"
    if "/bin/"             in p: return "u:object_r:system_file:s0"
    if "/etc/"             in p: return "u:object_r:system_file:s0"
    return "u:object_r:system_file:s0"

# ─── permission rules for new files ──────────────────────────────────────────
def default_perm(cfg_path, is_dir):
    """Return (uid, gid, mode_octal_str, selinux_ctx) for a new file."""
    p = cfg_path.lower()
    uid, gid, mode = 0, 0, "0755" if is_dir else "0644"
    ctx = _default_selinux(cfg_path)

    if is_dir:
        # All dirs default to 0755 root:root unless specific
        if "/vendor/" in p:
            uid, gid, mode = 0, 0, "0755"
        elif "/system_ext/" in p or p.startswith("system_ext"):
            uid, gid, mode = 0, 0, "0755"
        elif "/product/" in p or p.startswith("product"):
            uid, gid, mode = 0, 0, "0755"
        elif "/app/" in p or "/priv-app/" in p:
            uid, gid, mode = 1000, 1000, "0755"
        return uid, gid, mode, ctx

    # Files
    ext = os.path.splitext(cfg_path)[1].lower()

    if any(x in p for x in ["/bin/", "/xbin/", "/sbin/"]):
        uid, gid, mode = 0, 2000, "0755"
    elif "/vendor/bin/"  in p:
        uid, gid, mode = 0, 2000, "0755"
    elif any(x in p for x in ["/lib/", "/lib64/"]):
        uid, gid, mode = 0, 0, "0644"
    elif any(x in p for x in ["/app/", "/priv-app/"]):
        uid, gid, mode = 1000, 1000, "0644"
    elif "/framework/" in p:
        uid, gid, mode = 0, 0, "0644"
    elif "/etc/" in p or "/vendor/etc/" in p:
        uid, gid, mode = 0, 0, "0644"

    # Extension overrides
    if ext in (".sh", ".py", ".pl", ".rb"):
        mode = "0755"
    elif ext == ".so":
        mode = "0644"
    elif ext in (".apk", ".jar", ".apex"):
        mode = "0644"

    return uid, gid, mode, ctx

# ─── file list save / new-file detection ─────────────────────────────────────
def save_file_list(fs_dir, meta_dir):
    list_path = os.path.join(meta_dir, "file_list.txt")
    entries = []
    meta_abs = os.path.realpath(meta_dir)
    for root, dirs, files in walk_real(fs_dir, skip_abs={meta_abs}):
        rel = root[len(fs_dir):]
        entries.append(rel or "/")
        for fname in files:
            fp = os.path.join(root, fname)
            entries.append(fp[len(fs_dir):])
    with open(list_path, "w") as f:
        f.write("\n".join(entries) + "\n")
    ok(f"Saved file list: {len(entries)} entries")

def update_configs_for_new_files(fs_dir, meta_dir, mount_point):
    list_path  = os.path.join(meta_dir, "file_list.txt")
    cfg_path   = os.path.join(meta_dir, "fs_config.txt")
    ctx_path   = os.path.join(meta_dir, "file_contexts.txt")

    # Load original file list (paths are relative to whatever fs_dir was at unpack time)
    original = set()
    if os.path.isfile(list_path):
        with open(list_path) as f:
            original = set(line.strip() for line in f if line.strip())

    part_prefix = mount_point.lstrip("/") or "system"
    new_count = 0
    cfg_new = []
    ctx_new = []
    # Track clean cfg paths already written to avoid duplicates when a directory
    # contains both a dirty-named file (e.g. libfoo.so\x20) and its clean copy.
    added_cfg_paths: set = set()

    meta_abs = os.path.realpath(meta_dir)
    for root, dirs, files in walk_real(fs_dir, skip_abs={meta_abs}):
        rel = root[len(fs_dir):]
        if not rel:
            rel = "/"

        # Check if this path was in the original snapshot
        if rel not in original and rel != "/":
            cfg_path_entry = part_prefix + rel
            uid, gid, mode, ctx = default_perm(cfg_path_entry, is_dir=True)
            cfg_new.append(f"{cfg_path_entry} {uid} {gid} {mode} 0")
            ctx_rel     = mount_point + rel
            ctx_rel_esc = ctx_rel.replace(".", "\\.").replace("[","\\[").replace("]","\\]")
            ctx_new.append(f"{ctx_rel_esc}    -d    {ctx}")
            log(f"  [NEW DIR]  {cfg_path_entry}  {uid}:{gid} {mode}")
            new_count += 1
        for fname in sorted(files):
            fp   = os.path.join(root, fname)
            frel = fp[len(fs_dir):]
            if frel not in original:
                # Use clean filename in cfg/ctx so it matches what staging will contain
                # (staging uses _clean_filename on every file).  Dirty names (trailing
                # spaces / non-ASCII bytes from imperfect ext4 extraction) must NOT
                # appear in fs_config or make_ext4fs will fail to find the file.
                clean_fname = _clean_filename(fname)
                frel_cfg    = frel[:-len(fname)] + clean_fname if clean_fname != fname else frel
                if frel_cfg in added_cfg_paths:
                    continue   # skip dirty duplicate of a clean name already written
                added_cfg_paths.add(frel_cfg)
                cfg_path_entry = part_prefix + frel_cfg
                uid, gid, mode, ctx = default_perm(cfg_path_entry, is_dir=False)
                cfg_new.append(f"{cfg_path_entry} {uid} {gid} {mode} 0")
                ctx_rel     = mount_point + frel_cfg
                ctx_rel_esc = ctx_rel.replace(".", "\\.").replace("[","\\[").replace("]","\\]")
                # Detect symlinks vs regular files
                try:
                    import stat as _stat
                    ftype = "-l" if _stat.S_ISLNK(os.lstat(fp).st_mode) else "--"
                except Exception:
                    ftype = "--"
                ctx_new.append(f"{ctx_rel_esc}    {ftype}    {ctx}")
                log(f"  [NEW FILE] {cfg_path_entry}  {uid}:{gid} {mode}")
                new_count += 1

    if cfg_new:
        with open(cfg_path, "a") as f:
            f.write("\n".join(cfg_new) + "\n")
        with open(ctx_path, "a") as f:
            f.write("\n".join(ctx_new) + "\n")
        ok(f"Added {new_count} new entries to fs_config + file_contexts")
    else:
        log("No new files detected — using saved fs_config as-is")

# ─── UNPACK ───────────────────────────────────────────────────────────────────
def _detect_nested_content(files_dir, part_name):
    """
    Some system.img images contain a single nested subdir (e.g. system/system/).
    Detect this and return the actual content root.

    Heuristic: if files_dir contains ONLY one subdirectory whose name matches
    the partition name (e.g. "system"), and that subdir has real content, then
    the actual files are inside that subdir.

    Returns (content_dir, nested_subdir_name or None).
    """
    try:
        entries = [e for e in os.listdir(files_dir)
                   if not e.startswith(".")]
        if len(entries) == 1 and entries[0] == part_name:
            candidate = os.path.join(files_dir, entries[0])
            if os.path.isdir(candidate) and not os.path.islink(candidate):
                sub_entries = os.listdir(candidate)
                if len(sub_entries) > 3:   # has real content
                    return candidate, entries[0]
    except Exception:
        pass
    return files_dir, None

def cmd_unpack(img_path, part_name=None, out_dir=None):
    if not os.path.isfile(img_path):
        raise FileNotFoundError(f"Image not found: {img_path}")

    if not part_name:
        part_name = os.path.basename(img_path).replace(".img", "")
    if not out_dir:
        out_dir = os.path.join(WORK_DIR, part_name)

    # ── New layout ────────────────────────────────────────────────────────────
    # workspace/system/
    #   META/       ← tool metadata (fs_config, file_contexts, image_info, file_list)
    #   files/      ← extracted partition content (no META contamination)
    #
    # If the image has a nested system/system/ layout, files/ will contain a
    # single "system" subdir — we record this in META so repack knows.
    meta_dir  = os.path.join(out_dir, "META")
    files_dir = os.path.join(out_dir, "files")

    makedirs(out_dir)
    makedirs(meta_dir)
    makedirs(files_dir)

    # 1. Detect format
    fmt = detect_format(img_path)
    log(f"Detected format: {fmt}")

    if fmt == "super":
        raise RuntimeError("This looks like a super.img — use Super Unpack instead.")

    # 2. Convert sparse → raw if needed
    was_sparse = False
    raw_img    = os.path.join(out_dir, f"{part_name}.raw.img")

    if fmt == "sparse":
        was_sparse = True
        to_raw(img_path, raw_img)
    else:
        raw_img = img_path

    # 3. fsck
    log("Running e2fsck …")
    subprocess.run(["sudo", "e2fsck", "-fy", raw_img],
                   stdout=open(LOG_FILE,"a"), stderr=subprocess.STDOUT)

    # 4. Save metadata
    info = save_image_meta(raw_img, meta_dir, part_name)
    if was_sparse:
        set_meta(meta_dir, "original_was_sparse", "1")
        set_meta(meta_dir, "raw_img_path", raw_img)
    else:
        set_meta(meta_dir, "raw_img_path", raw_img)

    mount_point = info["mount_point"]

    # 5. Mount and extract to files/
    mnt_tmp = tempfile.mkdtemp(prefix="rom_mnt_")
    try:
        log(f"Mounting {raw_img} …")
        sudo_run(["mount", "-o", "loop,ro", raw_img, mnt_tmp])

        log("Copying files → files/ …")
        r = subprocess.run(
            ["sudo", "cp", "-a", "--preserve=all",
             mnt_tmp + "/.", files_dir + "/"],
            stderr=subprocess.PIPE
        )
        if r.returncode != 0:
            warn("cp failed, trying rsync …")
            subprocess.run(
                ["sudo", "rsync", "-aAX", mnt_tmp + "/", files_dir + "/"],
                check=True
            )

        # 6. Extract configs while mounted (xattrs still accessible)
        extract_fs_config(mnt_tmp, meta_dir, mount_point)

    finally:
        subprocess.run(["sudo", "umount", "-l", mnt_tmp],
                       stderr=subprocess.DEVNULL)
        try:
            os.rmdir(mnt_tmp)
        except Exception:
            pass

    # 7. Detect nested system/system layout
    content_dir, nested = _detect_nested_content(files_dir, part_name)
    if nested:
        set_meta(meta_dir, "nested_subdir", nested)
        log(f"Nested layout detected: files/{nested}/ is the actual content")
        ok(f"Edit content in: {content_dir}")
    else:
        set_meta(meta_dir, "nested_subdir", "")

    # 8. Fix ownership
    own(files_dir)
    own(meta_dir)
    own(out_dir)

    # 9. Save file list snapshot (from actual content root)
    save_file_list(content_dir, meta_dir)

    ok("═" * 50)
    ok(f"Unpack complete: {part_name}")
    print(f"\n  {W}Work dir  :{N} {out_dir}")
    print(f"  {W}Metadata  :{N} {meta_dir}")
    print(f"  {W}Content   :{N} {content_dir}")
    if nested:
        print(f"  {Y}⚠ Nested layout — edit files inside:{N}")
        print(f"  {C}{content_dir}{N}")
    else:
        print(f"\n  {Y}Edit files inside:{N}")
        print(f"  {C}{files_dir}{N}")
    print()


# ─── REPACK ───────────────────────────────────────────────────────────────────
# ─── fs_config / file_contexts pruning ───────────────────────────────────────
def normalize_fsconfig_paths(meta_dir):
    """Rewrite META/fs_config.txt so every path component is clean.

    Ext4 images extracted from ROM zips sometimes contain filenames with
    trailing garbage bytes (spaces, ASCII control chars, non-ASCII bytes from
    imperfect inode reads).  Those dirty names end up verbatim in fs_config.txt
    at unpack time.  Later, _hardlink_tree() stages files under CLEAN names,
    so make_ext4fs cannot match the clean staged file to the dirty fs_config
    entry and aborts with "failed to find [/path/with/ ] in canned fs_config".

    This function cleans every path component in the file using _clean_filename
    and deduplicates the result so no two entries share the same path key.
    """
    cfg_path = os.path.join(meta_dir, "fs_config.txt")
    if not os.path.isfile(cfg_path):
        return

    clean_lines = []
    seen_keys   = set()
    fixed = 0

    with open(cfg_path) as f:
        lines = f.readlines()

    for line in lines:
        stripped = line.rstrip("\n")
        if not stripped.strip():
            clean_lines.append(line)
            continue

        # fs_config format: <path> <uid> <gid> <mode> [capabilities]
        parts = stripped.split(None, 4)
        if not parts:
            clean_lines.append(line)
            continue

        orig_path = parts[0]
        # Clean each path component
        components = orig_path.split("/")
        cleaned_components = [_clean_filename(c) for c in components]
        clean_path = "/".join(cleaned_components)

        if clean_path != orig_path:
            fixed += 1
            parts[0] = clean_path

        # Dedup by clean path key
        if clean_path in seen_keys:
            continue
        seen_keys.add(clean_path)

        clean_lines.append(" ".join(parts) + "\n")

    if fixed:
        with open(cfg_path, "w") as f:
            f.writelines(clean_lines)
        ok(f"Normalized fs_config: {fixed} dirty path component(s) cleaned, {len(clean_lines)} entries total")
    else:
        log("fs_config paths already clean — no normalization needed")


def prune_deleted_from_configs(fs_dir, meta_dir, mount_point):
    """Remove entries from fs_config + file_contexts for files that no longer exist."""
    cfg_path = os.path.join(meta_dir, "fs_config.txt")
    ctx_path = os.path.join(meta_dir, "file_contexts.txt")
    part_prefix = mount_point.lstrip("/") or "system"

    # Build set of all current paths (as they appear in fs_config: prefix+rel)
    existing = set()
    meta_abs = os.path.realpath(meta_dir)
    for root, dirs, files in walk_real(fs_dir, skip_abs={meta_abs}):
        rel = root[len(fs_dir):]
        cfg_key = (part_prefix + rel).lstrip("/")
        existing.add(cfg_key)
        for fname in files:
            fp   = os.path.join(root, fname)
            frel = fp[len(fs_dir):]
            existing.add((part_prefix + frel).lstrip("/"))

    pruned_cfg = 0
    pruned_ctx = 0

    # Prune fs_config
    if os.path.isfile(cfg_path):
        kept = []
        with open(cfg_path) as f:
            for line in f:
                stripped = line.strip()
                if not stripped:
                    continue
                entry_path = stripped.split()[0] if stripped.split() else ""
                if entry_path in existing or entry_path == part_prefix:
                    kept.append(line)
                else:
                    log(f"  [PRUNED cfg] {entry_path}")
                    pruned_cfg += 1
        with open(cfg_path, "w") as f:
            f.writelines(kept)

    OTHER_PARTS = {"vendor","product","system_ext","odm","oem","apex","data","cache","proc","sys","dev"}

    # Prune file_contexts
    if os.path.isfile(ctx_path):
        kept = []
        with open(ctx_path) as f:
            for line in f:
                stripped = line.strip()
                if not stripped or stripped.startswith("#"):
                    kept.append(line)
                    continue
                # ctx line: /mount/path [ftype] u:object_r:...
                ctx_path_raw = stripped.split()[0] if stripped.split() else ""
                # Normalize to fs_config key (un-escape regex, then add part_prefix)
                bare = ctx_path_raw.lstrip("/").replace("\\.", ".").replace("\\[","[").replace("\\]","]")
                first_seg = bare.split("/")[0] if bare else ""
                if first_seg == part_prefix or first_seg in OTHER_PARTS:
                    chk = bare        # already has a known partition prefix
                elif bare:
                    chk = part_prefix + "/" + bare  # bare path (from mount_point="/") → add prefix
                else:
                    chk = part_prefix
                if chk in existing or chk == part_prefix:
                    kept.append(line)
                else:
                    pruned_ctx += 1
        with open(ctx_path, "w") as f:
            f.writelines(kept)

    if pruned_cfg or pruned_ctx:
        ok(f"Pruned {pruned_cfg} deleted entries from fs_config, {pruned_ctx} from file_contexts")
    else:
        log("No deleted entries to prune")

# ─── REPACK ENGINE ────────────────────────────────────────────────────────────
REPACK_METHODS = {
    "1": "make_ext4fs   (recommended — Android-native, most compatible)",
    "2": "mke2fs -d     (populate directly, no e2fsdroid needed)",
    "3": "mke2fs + e2fsdroid  (two-step, legacy compat)",
}

def _calc_image_size(fs_dir, meta_dir, block_size, orig_size, extra_mb=0, exclude_paths=None):
    """Return (new_size_bytes, block_count)."""
    meta_abs = os.path.realpath(meta_dir)
    # Build a find-based size calc that excludes META and any raw image files
    exclude_paths = set(exclude_paths or [])
    exclude_paths.add(meta_abs)

    # Use find + awk to sum up file sizes, skipping excluded paths
    # We can't use du --exclude reliably (it matches by name, not full path)
    total = 0
    for root, dirs, files in walk_real(fs_dir, skip_abs=exclude_paths):
        # Also skip any excluded directories
        dirs[:] = [d for d in dirs
                   if os.path.realpath(os.path.join(root, d)) not in exclude_paths]
        for fname in files:
            fp = os.path.join(root, fname)
            rp = os.path.realpath(fp)
            if rp in exclude_paths:
                continue
            try:
                total += os.lstat(fp).st_size
            except Exception:
                pass
        # Count dir itself (approximate inode overhead: 4KB per dir)
        total += 4096

    if total == 0:
        # fallback: du -sb
        r = subprocess.run(
            ["du", "-sb", "--exclude", meta_abs, fs_dir],
            capture_output=True, text=True
        )
        total = int(r.stdout.split()[0]) if r.returncode == 0 else 0

    headroom = max(total // 5, 32 * 1024 * 1024)
    new_size  = total + headroom + extra_mb * 1024 * 1024
    new_size  = ((new_size + block_size - 1) // block_size) * block_size
    if new_size < orig_size:
        new_size = orig_size
    return new_size, new_size // block_size

def _repack_make_ext4fs(fs_dir, raw_out, mount_point, cfg_path, ctx_path,
                        label, block_size, inode_size, new_size,
                        read_only=True):
    """Method 1: make_ext4fs — create + populate in one shot."""
    mk = require_tool("make_ext4fs")
    cmd = [mk,
           "-l", str(new_size),
           "-b", str(block_size),
           "-I", str(inode_size),
           "-T", "0",
           "-a", mount_point]
    if label and label not in ("<none>", ""):
        cmd += ["-L", label]
    if os.path.isfile(cfg_path):
        cmd += ["-C", cfg_path]
    if os.path.isfile(ctx_path):
        cmd += ["-S", ctx_path]
    if read_only:
        pass   # make_ext4fs creates read-only compatible images by default
    else:
        cmd += ["-w"]   # writable
    cmd += [raw_out, fs_dir]
    run(cmd)

def _repack_mke2fs_d(fs_dir, raw_out, mount_point, cfg_path, ctx_path,
                     label, block_size, inode_size, block_count, read_only=True):
    """Method 2: mke2fs -d  (populate directly, no e2fsdroid needed)."""
    mke2fs = require_tool("mke2fs")
    # Android-compatible feature set
    disable_features = [
        "^metadata_csum", "^64bit", "^huge_file",
        "^metadata_csum_seed", "^orphan_file",
    ]
    cmd = [
        mke2fs,
        "-t", "ext4",
        "-b", str(block_size),
        "-I", str(inode_size),
        "-m", "0",
        "-O", ",".join(disable_features),
        "-E", "lazy_itable_init=0,lazy_journal_init=0",
        "-d", fs_dir,
    ]
    if label and label not in ("<none>", ""):
        cmd += ["-L", label]
    if not read_only:
        cmd += ["-E", "test_fs"]
    cmd += [raw_out, str(block_count)]
    run(cmd)

def _repack_mke2fs_e2fsdroid(fs_dir, raw_out, mount_point, cfg_path, ctx_path,
                              label, block_size, inode_size, block_count,
                              read_only=True):
    """Method 3: mke2fs (empty) + e2fsdroid (populate)."""
    mke2fs    = require_tool("mke2fs")
    e2fsdroid = require_tool("e2fsdroid")

    # Create EMPTY ext4 with Android-compatible features (no metadata_csum!)
    disable_features = [
        "^metadata_csum", "^64bit", "^huge_file",
        "^metadata_csum_seed", "^orphan_file",
        "^dir_index",
    ]
    mk_cmd = [
        mke2fs,
        "-t", "ext4",
        "-b", str(block_size),
        "-I", str(inode_size),
        "-m", "0",
        "-O", ",".join(disable_features),
        "-E", "lazy_itable_init=0,lazy_journal_init=0",
    ]
    if label and label not in ("<none>", ""):
        mk_cmd += ["-L", label]
    mk_cmd += [raw_out, str(block_count)]
    run(mk_cmd)

    # Populate with e2fsdroid
    e2_cmd = [e2fsdroid, "-f", fs_dir, "-T", "0"]
    if os.path.isfile(cfg_path):
        e2_cmd += ["-C", cfg_path]
    if os.path.isfile(ctx_path):
        e2_cmd += ["-S", ctx_path]
    e2_cmd += ["-a", mount_point, raw_out]
    run(e2_cmd)

VALID_FTYPES = {"--", "-d", "-l", "-p", "-s", "-c", "-b"}

def repair_file_contexts(ctx_path):
    """
    Repairs file_contexts in-place:
    1. Drops blank/incomplete lines (< 2 fields)
    2. Fixes missing ftype column  (/path label → /path -- label)
    3. Fixes invalid/garbage ftype (/path JUNK label → /path -- label)
    4. Deduplicates — keeps only the FIRST entry per path (last-write
       duplicates from ensure_complete_fsconfig and original extraction
       cause "Multiple different specifications" errors)
    """
    if not os.path.isfile(ctx_path):
        return
    lines_out = []
    repaired = 0
    dropped  = 0
    deduped  = 0
    seen_paths = {}   # path → line index in lines_out

    with open(ctx_path) as f:
        raw_lines = f.readlines()

    for line in raw_lines:
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            lines_out.append(line)
            continue
        parts = stripped.split()
        if len(parts) < 2:
            dropped += 1
            continue

        path_key = parts[0]

        # Determine if ftype column is present and valid
        if len(parts) >= 3 and parts[1] in VALID_FTYPES:
            # Well-formed: /path  ftype  label
            ftype = parts[1]
            label = " ".join(parts[2:])
        elif len(parts) >= 2 and parts[1] not in VALID_FTYPES:
            # ftype is missing — second token is the label
            ftype = "--"
            label = " ".join(parts[1:])
            repaired += 1
        else:
            dropped += 1
            continue

        # Validate label looks like a SELinux context
        if not label.startswith("u:object_r:") and not label.startswith("<<none>>"):
            dropped += 1
            continue

        clean = f"{path_key}    {ftype}    {label}\n"

        # Deduplicate: if we've seen this path before, skip the new entry
        if path_key in seen_paths:
            deduped += 1
            continue

        seen_paths[path_key] = len(lines_out)
        lines_out.append(clean)

    with open(ctx_path, "w") as f:
        f.writelines(lines_out)

    if repaired or dropped or deduped:
        ok(f"file_contexts: {repaired} ftype fixes, {dropped} bad lines removed, "
           f"{deduped} duplicates removed  →  {len(seen_paths)} unique entries")

def ensure_complete_fsconfig(staging_dir, cfg_path, ctx_path, mount_point):
    """
    Walk staging_dir and guarantee EVERY path has an entry in fs_config.txt.
    Only adds stubs for paths missing from fs_config.
    Does NOT touch file_contexts for paths already listed there — dedup
    is handled by repair_file_contexts which runs after this.
    """
    import stat as _stat

    part_prefix = mount_point.lstrip("/") or "system"

    # Load known fs_config paths
    known_cfg = set()
    if os.path.isfile(cfg_path):
        with open(cfg_path) as f:
            for line in f:
                s = line.strip()
                if s:
                    known_cfg.add(s.split()[0])

    # Load known file_contexts paths (to avoid duplicate ctx entries)
    known_ctx = set()
    if os.path.isfile(ctx_path):
        with open(ctx_path) as f:
            for line in f:
                s = line.strip()
                if s and not s.startswith("#"):
                    parts = s.split()
                    if parts:
                        known_ctx.add(parts[0])

    cfg_new = []
    ctx_new = []

    for root, dirs, files in walk_real(staging_dir):
        rel = root[len(staging_dir):]

        cfg_key = part_prefix + rel if rel else part_prefix
        # Always write canonical /part_name/... form (never // form)
        ctx_rel = "/" + part_prefix + rel if rel else "/" + part_prefix
        ctx_key = _escape_fc_path(ctx_rel)

        if cfg_key not in known_cfg:
            cfg_new.append(f"{cfg_key} 0 0 0755 0")
            known_cfg.add(cfg_key)
            if ctx_key not in known_ctx:
                ctx_new.append(f"{ctx_key}    -d    u:object_r:system_file:s0\n")
                known_ctx.add(ctx_key)
            log(f"  [stub dir] {cfg_key}")

        for fname in sorted(files):
            fp      = os.path.join(root, fname)
            frel    = fp[len(staging_dir):]
            cfg_key = part_prefix + frel
            ctx_rel = "/" + part_prefix + frel
            ctx_key = _escape_fc_path(ctx_rel)

            if cfg_key not in known_cfg:
                try:
                    m = os.lstat(fp).st_mode
                    if _stat.S_ISLNK(m):    ftype, mode = "-l", "0777"
                    elif _stat.S_ISDIR(m):  ftype, mode = "-d", "0755"
                    else:                   ftype, mode = "--", "0644"
                except Exception:
                    ftype, mode = "--", "0644"

                cfg_new.append(f"{cfg_key} 0 0 {mode} 0")
                known_cfg.add(cfg_key)
                if ctx_key not in known_ctx:
                    ctx_new.append(f"{ctx_key}    {ftype}    u:object_r:system_file:s0\n")
                    known_ctx.add(ctx_key)
                log(f"  [stub file] {cfg_key}")

    if cfg_new:
        with open(cfg_path, "a") as f:
            f.write("\n".join(cfg_new) + "\n")
        if ctx_new and os.path.isfile(ctx_path):
            with open(ctx_path, "a") as f:
                f.writelines(ctx_new)
        ok(f"Added {len(cfg_new)} missing fs_config stubs")

def _norm_ctx_path(raw_path, prefix, part, OTHER_PARTS):
    """Normalise a file_contexts path to /prefix/... form."""
    bare = raw_path.lstrip("/")
    first_seg = bare.split("/")[0] if bare else ""
    if first_seg == part or first_seg in OTHER_PARTS:
        return "/" + bare
    elif bare:
        return prefix + "/" + bare
    return prefix

def generate_explicit_file_contexts(ctx_path, staging_dir, ext4_mountpoint, tmp_ctx_path):
    """
    Generate a per-file explicit file_contexts by walking staging_dir.

    Rather than trying to fix-up the stored regex-based file_contexts
    (which has proven fragile across many iterations), this function:

    1. Loads the stored file_contexts as compiled regex patterns with labels
    2. Walks every file/dir in staging_dir
    3. For each path, finds the best matching pattern (most specific = longest
       literal prefix) and assigns its label
    4. Falls back to _default_selinux() if no pattern matches
    5. Writes one explicit '/system/path  ftype  label' line per file

    This guarantees:
      • Every file has exactly one entry  (no "cannot lookup" crashes)
      • All paths start with ext4_mountpoint  (no prefix mismatches)
      • No duplicate entries  (each path is written exactly once)
    """
    import re as _re
    import stat as _stat

    prefix = ext4_mountpoint       # "/system"
    part   = prefix.lstrip("/")    # "system"
    OTHER_PARTS = {
        "vendor", "product", "system_ext", "odm", "oem",
        "apex", "data", "cache", "proc", "sys", "dev",
    }

    # ── 1. Load patterns from stored file_contexts ────────────────────────────
    patterns = []   # list of (normalised_literal_prefix, compiled_re, ftype, label)
    if os.path.isfile(ctx_path):
        with open(ctx_path) as f:
            for line in f:
                stripped = line.strip()
                if not stripped or stripped.startswith("#"):
                    continue
                parts_line = stripped.split()
                if len(parts_line) < 2:
                    continue
                raw_path = parts_line[0]
                if len(parts_line) >= 3 and parts_line[1] in VALID_FTYPES:
                    ftype_p = parts_line[1]
                    label_p = " ".join(parts_line[2:])
                else:
                    ftype_p = "--"
                    label_p = " ".join(parts_line[1:])

                if not label_p.startswith("u:object_r:") and label_p != "<<none>>":
                    continue

                norm = _norm_ctx_path(raw_path, prefix, part, OTHER_PARTS)
                # Anchor pattern — make_ext4fs matches full path
                pat_str = norm + "$"
                try:
                    compiled = _re.compile(pat_str)
                    # literal prefix = portion before first regex metachar
                    lit = _re.split(r'[.*()\[\]?+\\]', norm)[0]
                    patterns.append((lit, compiled, ftype_p, label_p))
                except _re.error:
                    pass

    # Sort longest literal prefix first → most specific patterns win
    patterns.sort(key=lambda x: len(x[0]), reverse=True)

    def lookup_label(abs_path):
        """Return the best SELinux label for abs_path from stored patterns."""
        for (_, pat_re, _pftype, plabel) in patterns:
            try:
                if pat_re.match(abs_path):
                    return plabel
            except Exception:
                continue
        return _default_selinux(abs_path)

    # ── 2. Walk staging dir → one explicit entry per path ─────────────────────
    out_lines = []
    seen      = set()
    count     = 0

    for root, dirs, files in walk_real(staging_dir):
        rel          = root[len(staging_dir):]
        abs_path     = (prefix + rel) if rel else prefix   # raw — for pattern lookup
        abs_path_esc = _escape_fc_path(abs_path)           # escaped — written to file

        if abs_path not in seen:
            lbl = lookup_label(abs_path)
            # For the partition root, make_ext4fs looks up the path WITH a
            # trailing slash (e.g. "/product/").  Add an optional-slash suffix
            # so the pattern matches both "/product" and "/product/".
            if not rel:
                root_pattern = abs_path_esc + "/?"
            else:
                root_pattern = abs_path_esc
            out_lines.append(f"{root_pattern}    -d    {lbl}\n")
            seen.add(abs_path)
            count += 1

        for fname in sorted(files):
            fp           = os.path.join(root, fname)
            frel         = fp[len(staging_dir):]
            abs_path     = prefix + frel
            abs_path_esc = _escape_fc_path(abs_path)

            if abs_path in seen:
                continue

            # Derive ftype from disk — NEVER from the pattern
            try:
                m = os.lstat(fp).st_mode
                if _stat.S_ISLNK(m):    ftype_h = "-l"
                elif _stat.S_ISDIR(m):  ftype_h = "-d"
                elif _stat.S_ISFIFO(m): ftype_h = "-p"
                elif _stat.S_ISSOCK(m): ftype_h = "-s"
                elif _stat.S_ISCHR(m):  ftype_h = "-c"
                elif _stat.S_ISBLK(m):  ftype_h = "-b"
                else:                   ftype_h = "--"
            except Exception:
                ftype_h = "--"

            lbl = lookup_label(abs_path)
            out_lines.append(f"{abs_path_esc}    {ftype_h}    {lbl}\n")
            seen.add(abs_path)
            count += 1

    with open(tmp_ctx_path, "w") as fout:
        fout.writelines(out_lines)

    ok(f"Generated explicit file_contexts: {count} entries "
       f"(prefix={prefix}, patterns={len(patterns)})")


def _clean_filename(name):
    """Strip trailing whitespace and non-printable / non-ASCII bytes from a
    filename.

    Extracted ROM images sometimes contain filenames with trailing garbage
    bytes (spaces, control characters, high bytes from incomplete inode reads).
    These cause make_ext4fs to fail because the file appears in the staging
    directory under a name that has no matching fs_config entry:

        failed to find [/system/.../libc_malloc_debug.so ] in canned fs_config

    We strip anything whose ordinal is < 0x21 (space + all control chars)
    or > 0x7e (DEL + non-ASCII) from the right end of the name.
    Normal Android filenames always end in an alphanumeric or '.' character.
    """
    result = name
    while result and (ord(result[-1]) < 0x21 or ord(result[-1]) > 0x7e):
        result = result[:-1]
    return result if result else name


def _hardlink_tree(src_dir, dst_dir):
    """Recursively hardlink src_dir → dst_dir. Falls back to copy for cross-device."""
    os.makedirs(dst_dir, exist_ok=True)
    for item in os.listdir(src_dir):
        s = os.path.join(src_dir, item)
        clean_item = _clean_filename(item)
        d = os.path.join(dst_dir, clean_item)
        if os.path.islink(s):
            # Reproduce symlink (skip if dest already exists — e.g. a stub)
            if not os.path.exists(d) and not os.path.islink(d):
                link_target = os.readlink(s)
                os.symlink(link_target, d)
        elif os.path.isdir(s):
            _hardlink_tree(s, d)
        else:
            if os.path.exists(d):
                # Clean name already present (created as a stub); skip the
                # garbage-named duplicate so make_ext4fs doesn't encounter it.
                continue
            try:
                os.link(s, d)
            except OSError:
                shutil.copy2(s, d)

def cmd_repack(work_dir, output_img=None, method="1",
               read_only=True, extra_mb=0):
    meta_dir = os.path.join(work_dir, "META")

    if not os.path.isdir(work_dir):
        raise FileNotFoundError(f"Work directory not found: {work_dir}")
    if not os.path.isdir(meta_dir):
        raise FileNotFoundError(f"META directory missing — was this unpacked by this tool?")

    # ── Load metadata first ───────────────────────────────────────────────────
    info        = load_meta(meta_dir)
    part_name   = info.get("partition_name", "partition")
    mount_point = info.get("mount_point", "/")
    block_size  = int(info.get("block_size",  "4096"))
    inode_size  = int(info.get("inode_size",  "256"))
    was_sparse  = info.get("original_was_sparse", "0") == "1"
    label       = info.get("label", "")
    orig_size   = int(info.get("original_size", "0"))

    # ── Resolve fs_dir (content root) ────────────────────────────────────────
    # Priority order:
    #   1. New layout:    work_dir/files/[nested_subdir/]   (this version)
    #   2. Old v2 layout: work_dir/fs/                      (legacy compat)
    #   3. Old v1 layout: work_dir/                         (very old compat)
    files_dir  = os.path.join(work_dir, "files")
    legacy_fs  = os.path.join(work_dir, "fs")
    nested_sub = info.get("nested_subdir", "").strip()

    if os.path.isdir(files_dir):
        if nested_sub and os.path.isdir(os.path.join(files_dir, nested_sub)):
            fs_dir = os.path.join(files_dir, nested_sub)
            log(f"New layout (nested): content at files/{nested_sub}/")
        else:
            fs_dir = files_dir
            log(f"New layout: content at files/")
    elif os.path.isdir(legacy_fs):
        fs_dir = legacy_fs
        log(f"Legacy layout: content at fs/")
    else:
        fs_dir = work_dir
        log(f"Old layout: content at work_dir root")

    # ── The critical -a (mountpoint) argument for make_ext4fs / e2fsdroid ─────
    # fs_config entries are stored with the partition name as prefix:
    #   "system/bin/sh 0 2000 0755 0"   ← part_prefix = "system"
    #   "vendor/bin/foo 0 2000 0755 0"  ← part_prefix = "vendor"
    #
    # make_ext4fs computes the fs_config lookup key as:
    #   <mountpoint_arg> + "/" + <relative_path_from_srcdir>
    # stripping the leading "/".  So:
    #   -a /system  →  staging/bin/sh  →  lookup "system/bin/sh"  ✓
    #   -a /        →  staging/bin/sh  →  lookup "bin/sh"         ✗
    #
    # Rule: derive ext4_mountpoint from the fs_config prefix, not from
    # the runtime "Last mounted on" value stored in image_info.txt.
    part_prefix     = mount_point.lstrip("/") or part_name
    ext4_mountpoint = f"/{part_prefix}"   # e.g. "/system", "/vendor", "/product"
    log(f"fs_config prefix: '{part_prefix}'  →  -a {ext4_mountpoint}")

    if not output_img:
        parent = os.path.dirname(os.path.abspath(work_dir))
        output_img = os.path.join(parent, f"{part_name}_repacked.img")

    makedirs(os.path.dirname(output_img))

    cfg_path = os.path.join(meta_dir, "fs_config.txt")
    ctx_path = os.path.join(meta_dir, "file_contexts.txt")

    # 0. Normalize any dirty (garbage-byte) path components in existing fs_config
    log("Normalizing fs_config paths (strip garbage bytes) …")
    normalize_fsconfig_paths(meta_dir)

    # 1. Prune entries for deleted files
    log("Checking for deleted files …")
    prune_deleted_from_configs(fs_dir, meta_dir, mount_point)

    # 2. Add entries for new files
    log("Checking for new files …")
    update_configs_for_new_files(fs_dir, meta_dir, mount_point)

    # ── Build set of files to exclude from staging and size calc ────────────
    # For old layout (fs_dir = work_dir), *.raw.img files live alongside content.
    # Including them in staging would embed a 1GB+ file inside the new image.
    # Read raw_img_path from META; also skip any *.raw.img at the fs_dir root.
    raw_img_path = info.get("raw_img_path", "").strip()
    skip_from_staging = set()
    if raw_img_path:
        skip_from_staging.add(os.path.realpath(raw_img_path))
    # Also skip any *.raw.img or orphaned *.img at the root of fs_dir
    # (covers cases where raw_img_path wasn't recorded correctly)
    for _f in os.listdir(fs_dir):
        if _f.endswith(".raw.img") or (_f.endswith(".img") and _f.startswith(part_name)):
            skip_from_staging.add(os.path.realpath(os.path.join(fs_dir, _f)))

    if skip_from_staging:
        log(f"Staging will exclude {len(skip_from_staging)} image file(s): "
            + ", ".join(os.path.basename(p) for p in skip_from_staging))

    # 3. Calculate image size  (exclude meta and raw images)
    log(f"Calculating image size (extra: {extra_mb} MB) …")
    new_size, block_count = _calc_image_size(
        fs_dir, meta_dir, block_size, orig_size, extra_mb,
        exclude_paths=skip_from_staging
    )
    log(f"Image: {new_size // 1024 // 1024} MB  ({block_count} blocks × {block_size}B)")
    log(f"Mode: {'read-only' if read_only else 'read-write'}  Method: {REPACK_METHODS.get(method,'?')}")

    raw_out = (output_img.replace(".img", ".raw.img")
               if was_sparse else output_img)

    # ── Staging directory ─────────────────────────────────────────────────────
    staging = tempfile.mkdtemp(prefix="rom_stage_")
    try:
        log(f"Creating staging dir (hardlinks, no META/raw-imgs) …")
        meta_abs = os.path.realpath(meta_dir)
        for item in os.listdir(fs_dir):
            src = os.path.join(fs_dir, item)
            src_real = os.path.realpath(src)
            clean_item = _clean_filename(item)
            dst = os.path.join(staging, clean_item)
            # Skip META directory and raw image files
            if src_real == meta_abs or item == "META":
                continue
            if src_real in skip_from_staging:
                continue
            if os.path.isdir(src) and not os.path.islink(src):
                _hardlink_tree(src, dst)
            else:
                if os.path.exists(dst):
                    continue   # clean-named file already staged
                try:
                    os.link(src, dst)
                except OSError:
                    shutil.copy2(src, dst)

        src_dir = staging

        # Step A: add fs_config stubs for any paths missing from the config
        # (runtime dirs like /acct /proc /sys etc.)
        # Use ext4_mountpoint so stubs get the same "system/" prefix as real entries
        ensure_complete_fsconfig(staging, cfg_path, ctx_path, ext4_mountpoint)

        # Step B: repair + deduplicate file_contexts AFTER stubs are appended
        repair_file_contexts(ctx_path)

        # Step C: generate a per-file explicit file_contexts from staging dir.
        # Walks every path in staging, looks up labels from stored patterns,
        # writes one explicit '/system/path  ftype  label' per file.
        # This guarantees 100% coverage with correct prefix — no pattern gaps.
        tmp_ctx = ctx_path + ".norm.tmp"
        generate_explicit_file_contexts(ctx_path, staging, ext4_mountpoint, tmp_ctx)
        build_ctx = tmp_ctx

        # 4. Build image using selected method
        if method == "1":
            log("Building with make_ext4fs …")
            _repack_make_ext4fs(src_dir, raw_out, ext4_mountpoint, cfg_path, build_ctx,
                                label, block_size, inode_size, new_size, read_only)
        elif method == "2":
            log("Building with mke2fs -d …")
            _repack_mke2fs_d(src_dir, raw_out, ext4_mountpoint, cfg_path, build_ctx,
                             label, block_size, inode_size, block_count, read_only)
        elif method == "3":
            log("Building with mke2fs + e2fsdroid …")
            _repack_mke2fs_e2fsdroid(src_dir, raw_out, ext4_mountpoint, cfg_path, build_ctx,
                                     label, block_size, inode_size, block_count, read_only)
        else:
            raise ValueError(f"Unknown repack method: {method}")

    finally:
        # Always clean up staging dir and temp ctx
        for _p in [staging, ctx_path + ".norm.tmp"]:
            try:
                if os.path.isdir(_p):
                    shutil.rmtree(_p)
                elif os.path.isfile(_p):
                    os.remove(_p)
            except Exception:
                pass

    # 5. Convert back to sparse if original was sparse
    if was_sparse:
        log("Converting back to sparse …")
        to_sparse(raw_out, output_img)
        os.remove(raw_out)

    own(os.path.dirname(output_img))

    ok("═" * 50)
    ok(f"Repack complete: {part_name}")
    size_mb = os.path.getsize(output_img) // 1024 // 1024
    print(f"\n  {W}Output image :{N} {output_img}")
    print(f"  {W}Size         :{N} {size_mb} MB")
    print(f"  {W}Mode         :{N} {'read-only' if read_only else 'read-write'}\n")

# ─── SUPER UNPACK ─────────────────────────────────────────────────────────────
def cmd_super_unpack(super_img, out_dir=None):
    if not os.path.isfile(super_img):
        raise FileNotFoundError(f"super.img not found: {super_img}")

    if not out_dir:
        out_dir = os.path.join(WORK_DIR, "super_unpacked")

    meta_dir  = os.path.join(out_dir, "META")
    parts_dir = os.path.join(out_dir, "partitions")
    makedirs(meta_dir)
    makedirs(parts_dir)

    fmt = detect_format(super_img)
    log(f"Super image format: {fmt}")

    was_sparse = False
    raw_super  = os.path.join(out_dir, "super.raw.img")

    if fmt == "sparse":
        was_sparse = True
        to_raw(super_img, raw_super)
    else:
        raw_super = super_img

    # Save super metadata
    raw_size = os.path.getsize(raw_super)
    with open(os.path.join(meta_dir, "super_info.txt"), "w") as f:
        f.write(f"original_super={super_img}\n")
        f.write(f"was_sparse={1 if was_sparse else 0}\n")
        f.write(f"raw_super_size={raw_size}\n")

    # Try lpunpack methods
    log("Unpacking LP partitions …")
    unpacked = False

    # Method 1: system lpunpack
    if not unpacked and shutil.which("lpunpack"):
        r = subprocess.run(
            ["lpunpack", raw_super, parts_dir],
            stdout=open(LOG_FILE,"a"), stderr=subprocess.STDOUT
        )
        unpacked = r.returncode == 0

    # Method 2: bundled imgkit
    imgkit = tool("imgkit")
    if not unpacked and imgkit:
        r = subprocess.run(
            [imgkit, "lpunpack", raw_super, parts_dir],
            stdout=open(LOG_FILE,"a"), stderr=subprocess.STDOUT
        )
        unpacked = r.returncode == 0

    # Method 3: Python fallback
    if not unpacked:
        log("Using built-in Python LP extractor …")
        lpunpack_py(raw_super, parts_dir)
        unpacked = True

    own(out_dir)

    imgs = sorted(glob.glob(os.path.join(parts_dir, "*.img")))
    ok("═" * 50)
    ok("Super unpack complete")
    print(f"\n  {W}Partitions:{N}")
    for img in imgs:
        mb = os.path.getsize(img) // 1024 // 1024
        print(f"    {G}✓{N}  {os.path.basename(img):<30} {mb} MB")
    print(f"\n  {W}Next steps:{N}")
    print(f"  Use EXT4 Unpack on each .img in:")
    print(f"  {C}{parts_dir}{N}\n")
    return parts_dir

# ─── SUPER REPACK ─────────────────────────────────────────────────────────────
def cmd_super_repack(parts_dir, output_img=None, meta_dir=None):
    if not os.path.isdir(parts_dir):
        raise FileNotFoundError(f"Partitions directory not found: {parts_dir}")

    if not output_img:
        output_img = os.path.join(WORK_DIR, "super_repacked.img")
    if not meta_dir:
        meta_dir = os.path.join(os.path.dirname(parts_dir), "META")

    makedirs(os.path.dirname(output_img))

    was_sparse   = False
    super_size   = 0
    if os.path.isfile(os.path.join(meta_dir, "super_info.txt")):
        with open(os.path.join(meta_dir, "super_info.txt")) as f:
            for line in f:
                line = line.strip()
                if line.startswith("was_sparse="):
                    was_sparse = line.split("=",1)[1] == "1"
                elif line.startswith("raw_super_size="):
                    super_size = int(line.split("=",1)[1])

    lpmake = require_tool("lpmake")

    # Build partition list
    part_args = []
    total_size = 0

    for img in sorted(glob.glob(os.path.join(parts_dir, "*.img"))):
        pname = os.path.basename(img).replace(".img","")
        pfmt  = detect_format(img)

        # Get actual raw size
        if pfmt == "sparse":
            tmp = tempfile.mktemp(suffix=".raw.img")
            to_raw(img, tmp)
            psize = os.path.getsize(tmp)
            os.remove(tmp)
        else:
            psize = os.path.getsize(img)

        total_size += psize
        part_args += [
            "--partition", f"{pname}:readonly:{psize}:main",
            "--image",     f"{pname}={img}",
        ]
        log(f"  Partition: {pname:<20} {psize//1024//1024} MB")

    # Device size
    metadata_overhead = 65536 * 3 * 2
    device_size = total_size + metadata_overhead + 8 * 1024 * 1024
    device_size = ((device_size + 511) // 512) * 512
    if super_size and super_size > device_size:
        device_size = super_size

    log(f"Super device size: {device_size // 1024 // 1024} MB")

    cmd = [
        lpmake,
        "--metadata-size",  "65536",
        "--metadata-slots", "3",
        "--device",         f"super:{device_size}",
        "--group",          f"main:{device_size}",
    ] + part_args + ["--sparse", "--output", output_img]

    run(cmd)
    own(os.path.dirname(output_img))

    ok("═" * 50)
    ok("Super repack complete")
    print(f"\n  {W}Output:{N} {output_img}")
    print(f"  {W}Size  :{N} {os.path.getsize(output_img)//1024//1024} MB\n")

# ─── BATCH UNPACK ────────────────────────────────────────────────────────────
def cmd_batch_unpack(img_dir, out_base=None):
    if not out_base:
        out_base = WORK_DIR

    known = ["system","system_ext","product","vendor","odm","vendor_dlkm","odm_dlkm"]
    suffixes = ["", "_a", "_b"]
    found = []

    for name in known:
        for sfx in suffixes:
            candidate = os.path.join(img_dir, f"{name}{sfx}.img")
            if os.path.isfile(candidate):
                clean = name  # strip _a/_b for workdir
                found.append((candidate, clean, os.path.join(out_base, clean)))

    if not found:
        raise FileNotFoundError(f"No partition images found in: {img_dir}")

    log(f"Found {len(found)} partition images")
    for img_path, pname, outdir in found:
        print(f"\n  {C}━━━ {pname} ━━━{N}")
        try:
            cmd_unpack(img_path, pname, outdir)
        except Exception as e:
            err(f"Failed to unpack {pname}: {e}")

    ok(f"Batch unpack complete: {len(found)} images")

# ─── BATCH REPACK ─────────────────────────────────────────────────────────────
def cmd_batch_repack(workspace_dir, out_dir=None, per_partition_opts=None):
    """
    Repack all unpacked partitions found in workspace_dir.

    per_partition_opts: dict of {part_name: (method, read_only, extra_mb)}
                        Falls back to defaults if a partition isn't listed.
    out_dir: where to write repacked images (defaults to workspace_dir parent)
    """
    if not out_dir:
        out_dir = os.path.dirname(os.path.abspath(workspace_dir))

    per_partition_opts = per_partition_opts or {}
    makedirs(out_dir)

    # Find all subdirectories that look like unpacked partitions (have META/)
    candidates = []
    try:
        for entry in sorted(os.listdir(workspace_dir)):
            full = os.path.join(workspace_dir, entry)
            if os.path.isdir(full) and os.path.isdir(os.path.join(full, "META")):
                candidates.append((entry, full))
    except Exception as e:
        raise RuntimeError(f"Cannot list workspace: {e}")

    if not candidates:
        raise FileNotFoundError(f"No unpacked partitions found in: {workspace_dir}")

    log(f"Found {len(candidates)} partitions to repack: {[n for n,_ in candidates]}")

    results = []
    for pname, work_dir in candidates:
        print(f"\n  {C}━━━ {pname} ━━━{N}")
        method, read_only, extra_mb = per_partition_opts.get(pname, ("1", True, 0))
        output_img = os.path.join(out_dir, f"{pname}_repacked.img")
        try:
            cmd_repack(work_dir, output_img, method, read_only, extra_mb)
            results.append((pname, True, output_img))
        except Exception as e:
            err(f"Failed to repack {pname}: {e}")
            results.append((pname, False, str(e)))

    print(f"\n  {W}── Batch Repack Summary ──{N}")
    for pname, ok_flag, detail in results:
        if ok_flag:
            sz = os.path.getsize(detail) // 1024 // 1024
            print(f"  {G}✓{N}  {pname:<15} {sz} MB  →  {detail}")
        else:
            print(f"  {R}✗{N}  {pname:<15} FAILED: {detail}")
    print()

    n_ok  = sum(1 for _,o,_ in results if o)
    n_err = len(results) - n_ok
    ok(f"Batch repack complete: {n_ok} succeeded, {n_err} failed")


def cmd_info(img_path):
    if not os.path.isfile(img_path):
        raise FileNotFoundError(f"File not found: {img_path}")

    fmt  = detect_format(img_path)
    size = os.path.getsize(img_path)

    print(f"\n  {W}Image  :{N} {img_path}")
    print(f"  {W}Size   :{N} {size // 1024 // 1024} MB  ({size:,} bytes)")
    print(f"  {W}Format :{N} {fmt}")

    if fmt in ("ext4", "unknown"):
        r = subprocess.run(
            ["dumpe2fs", "-h", img_path],
            capture_output=True, text=True
        )
        if r.returncode == 0:
            interesting = [
                "Block size", "Block count", "Inode count",
                "Volume name", "Last mounted", "Filesystem features",
                "Filesystem UUID",
            ]
            print()
            for line in r.stdout.splitlines():
                for key in interesting:
                    if line.startswith(key + ":"):
                        print(f"  {DIM}{line}{N}")
    elif fmt == "sparse":
        print(f"\n  {Y}(Sparse image — convert to raw to read ext4 metadata){N}")
    elif fmt == "super":
        print(f"\n  {Y}(super.img — use Super Unpack to extract partitions){N}")
    print()

# ─── SETUP ────────────────────────────────────────────────────────────────────
def cmd_setup():
    check_ubuntu()

    # Mark bundled tools executable
    a = arch()
    bins_dir = os.path.join(SCRIPT_DIR, "binaries", "bin", "Linux", a)
    if os.path.isdir(bins_dir):
        for f in os.listdir(bins_dir):
            fp = os.path.join(bins_dir, f)
            if os.path.isfile(fp):
                os.chmod(fp, 0o755)
        ok(f"Bundled tools ready: {bins_dir}")
    else:
        warn(f"Bundled tools directory not found: {bins_dir}")

    makedirs(WORK_DIR)
    makedirs(TOOLS_DIR)

    # Install apt packages
    pkgs = [
        "e2fsprogs", "util-linux", "attr",
        "python3", "coreutils", "file", "bc", "pv",
    ]
    log("Updating apt …")
    subprocess.run(["sudo", "apt-get", "update", "-qq"],
                   stdout=open(LOG_FILE,"a"), stderr=subprocess.STDOUT)
    log(f"Installing: {' '.join(pkgs)}")
    subprocess.run(
        ["sudo", "apt-get", "install", "-y"] + pkgs,
        stdout=open(LOG_FILE,"a"), stderr=subprocess.STDOUT
    )

    # Try android-sdk-libsparse-utils for lpunpack
    r = subprocess.run(
        ["apt-cache", "show", "android-sdk-libsparse-utils"],
        capture_output=True
    )
    if r.returncode == 0:
        subprocess.run(
            ["sudo", "apt-get", "install", "-y", "android-sdk-libsparse-utils"],
            stdout=open(LOG_FILE,"a"), stderr=subprocess.STDOUT
        )

    ok("Setup complete!")
    own(WORK_DIR)

# ─── FIX PERMISSIONS ─────────────────────────────────────────────────────────
def cmd_fix_perms(target=None):
    target = target or WORK_DIR
    makedirs(target)
    log(f"Fixing permissions on: {target}")
    own(target)
    ok(f"Done — {target} is now writable by {REAL_USER}")

# ─── Python LP unpacker (fallback) ───────────────────────────────────────────
LP_METADATA_HEADER_MAGIC = 0x4D0CC467
LP_SECTOR_SIZE           = 512
HEADER_SIZE              = 80
PARTITION_SIZE           = 52
EXTENT_SIZE              = 20

def lpunpack_py(super_path, out_dir):
    os.makedirs(out_dir, exist_ok=True)
    log(f"LP Python extractor: scanning {super_path} …")

    with open(super_path, "rb") as f:
        scan_data = f.read(min(os.path.getsize(super_path), 128 * 1024 * 1024))

    meta_off = -1
    for offset in [4096, 8192, LP_SECTOR_SIZE, LP_SECTOR_SIZE * 2]:
        if offset + 4 <= len(scan_data):
            magic = struct.unpack_from("<I", scan_data, offset)[0]
            if magic == LP_METADATA_HEADER_MAGIC:
                meta_off = offset
                break

    if meta_off < 0:
        for off in range(0, min(len(scan_data), 0x200000), LP_SECTOR_SIZE):
            if off + 4 <= len(scan_data):
                if struct.unpack_from("<I", scan_data, off)[0] == LP_METADATA_HEADER_MAGIC:
                    meta_off = off
                    break

    if meta_off < 0:
        raise RuntimeError("LP metadata not found in super image")

    log(f"LP metadata at offset 0x{meta_off:x}")

    # Parse header (simplified flat struct)
    try:
        h = struct.unpack_from(
            "<IHH I 32s I 32s I 32s I I I I I I I Q I",
            scan_data, meta_off
        )
    except struct.error as e:
        raise RuntimeError(f"Failed to parse LP header: {e}")

    header = {
        "magic":                 h[0],
        "header_size":           h[3],
        "partitions_offset":     h[9],
        "partitions_count":      h[10],
        "extents_offset":        h[11],
        "extents_count":         h[12],
    }

    tables_off = meta_off + header["header_size"]

    # Parse partitions
    partitions = []
    base = tables_off + header["partitions_offset"]
    for i in range(header["partitions_count"]):
        off = base + i * PARTITION_SIZE
        if off + PARTITION_SIZE > len(scan_data):
            break
        name_b, attrs, first_ext, num_ext = struct.unpack_from("<36sIII", scan_data, off)
        name = name_b.rstrip(b"\x00").decode("utf-8", errors="replace")
        if name:
            partitions.append({
                "name": name,
                "first_extent_index": first_ext,
                "num_extents": num_ext,
            })

    # Parse extents
    extents = []
    base = tables_off + header["extents_offset"]
    for i in range(header["extents_count"]):
        off = base + i * EXTENT_SIZE
        if off + EXTENT_SIZE > len(scan_data):
            break
        num_sectors, target_type, target_data = struct.unpack_from("<QII", scan_data, off)
        extents.append({
            "num_sectors": num_sectors,
            "target_data": target_data,
        })

    log(f"Found {len(partitions)} partitions, {len(extents)} extents")

    # Extract each partition
    with open(super_path, "rb") as sf:
        for part in partitions:
            name   = part["name"]
            first  = part["first_extent_index"]
            count  = part["num_extents"]
            p_exts = extents[first:first + count]

            out_path = os.path.join(out_dir, name + ".img")
            log(f"  Extracting: {name} → {out_path}")

            with open(out_path, "wb") as of:
                for ext in p_exts:
                    offset = ext["target_data"] * LP_SECTOR_SIZE
                    length = ext["num_sectors"] * LP_SECTOR_SIZE
                    sf.seek(offset)
                    remaining = length
                    while remaining > 0:
                        chunk = min(remaining, 1024 * 1024)
                        buf = sf.read(chunk)
                        if not buf:
                            of.write(b"\x00" * chunk)
                        else:
                            of.write(buf)
                        remaining -= chunk

    ok(f"LP extraction done → {out_dir}")

# ─── MENU HELPERS ─────────────────────────────────────────────────────────────
def banner():
    os.system("clear")
    print(f"""{C}{BOLD}
  ╔══════════════════════════════════════════════════════╗
  ║   Android ROM Image Tool  v{VERSION}                     ║
  ║   ext4 · sparse · super.img · fs_config · SELinux   ║
  ╚══════════════════════════════════════════════════════╝{N}
""")
    print(f"  {DIM}File owner: {W}{REAL_USER}{DIM} (uid={REAL_UID}  gid={REAL_GID}){N}\n")

def ask(prompt_text, default=""):
    """Ask user a question, return answer (stripped). Falls back to default."""
    disp = f"  {C}{prompt_text}{N}"
    if default:
        disp += f" [{W}{default}{N}]"
    disp += ": "
    try:
        ans = input(disp).strip()
    except (EOFError, KeyboardInterrupt):
        print()
        return default
    return ans if ans else default

def pause():
    try:
        input(f"\n  {Y}Press Enter to continue …{N}")
    except (EOFError, KeyboardInterrupt):
        pass

def choose(prompt_text):
    """Read a menu choice."""
    try:
        return input(f"\n  {W}{prompt_text}{N}: ").strip()
    except (EOFError, KeyboardInterrupt):
        print()
        return "0"

def run_op(fn, *args, **kwargs):
    """Run an operation, catch errors, pause on completion."""
    try:
        fn(*args, **kwargs)
    except KeyboardInterrupt:
        print(f"\n  {Y}Cancelled.{N}")
    except Exception as e:
        err(str(e))
        print(f"\n  {R}Operation failed — see {LOG_FILE}{N}")
    pause()

# ─── EXT4 MENU ────────────────────────────────────────────────────────────────
def _ask_repack_options():
    """Interactively ask for repack method, rw/ro and extra MB."""
    print(f"\n  {W}── Repack Options ──{N}")
    print(f"  {DIM}Method:{N}")
    for k, v in REPACK_METHODS.items():
        print(f"    [{k}] {v}")
    method   = ask("Method", "1")
    if method not in REPACK_METHODS:
        method = "1"

    rw_ans   = ask("Read-only image? (y/n)", "y").lower()
    read_only = rw_ans != "n"

    extra_s  = ask("Extra MB to add beyond auto-calculated size", "0")
    try:
        extra_mb = int(extra_s)
    except ValueError:
        extra_mb = 0

    return method, read_only, extra_mb

def _ask_repack_options_for(part_name, default_method="1", default_ro=True, default_extra=0):
    """Ask repack options for a single partition. Returns (method, read_only, extra_mb)."""
    print(f"\n  {C}── Options for: {W}{part_name}{N}")
    print(f"  {DIM}Method:{N}")
    for k, v in REPACK_METHODS.items():
        print(f"    [{k}] {v}")
    method = ask("Method", default_method)
    if method not in REPACK_METHODS:
        method = "1"
    rw    = ask("Read-only? (y/n)", "y" if default_ro else "n").lower()
    extra = ask("Extra MB beyond auto-size", str(default_extra))
    try:
        extra_mb = int(extra)
    except ValueError:
        extra_mb = 0
    return method, rw != "n", extra_mb

def menu_ext4():
    while True:
        banner()
        print(f"  {W}── EXT4 Operations ──{N}\n")
        print("  [1]  Unpack image")
        print("  [2]  Repack image")
        print("  [3]  Quick Edit  (unpack → edit → repack)")
        print("  [4]  Batch Unpack  (all partitions from a directory)")
        print("  [5]  Batch Repack  (all unpacked partitions in workspace)")
        print("  [6]  Image Info")
        print("  [7]  Convert sparse → raw")
        print("  [8]  Convert raw → sparse")
        print("  [0]  Back")

        ch = choose("Choice")

        if ch == "1":
            banner()
            img  = ask("Image path")
            if not img: continue
            name = ask("Partition name", os.path.basename(img).replace(".img",""))
            out  = ask("Output directory", os.path.join(WORK_DIR, name))
            run_op(cmd_unpack, img, name, out)

        elif ch == "2":
            banner()
            wd = ask("Work directory (contains META/ and files/)")
            if not wd: continue
            parent = os.path.dirname(os.path.abspath(wd))
            out = ask("Output image path",
                      os.path.join(parent, os.path.basename(wd) + "_repacked.img"))
            method, read_only, extra_mb = _ask_repack_options()
            run_op(cmd_repack, wd, out, method, read_only, extra_mb)

        elif ch == "3":
            banner()
            img  = ask("Image path")
            if not img: continue
            name = ask("Partition name", os.path.basename(img).replace(".img",""))
            wd   = os.path.join(WORK_DIR, name)
            try:
                cmd_unpack(img, name, wd)
            except Exception as e:
                err(str(e)); pause(); continue
            content = os.path.join(wd, "files")
            meta    = load_meta(os.path.join(wd, "META"))
            nested  = meta.get("nested_subdir","").strip()
            show_path = os.path.join(content, nested) if nested else content
            print(f"\n  {Y}Edit files in:{N}  {C}{show_path}{N}")
            input(f"  {Y}Press Enter when done to repack …{N}")
            parent = os.path.dirname(os.path.abspath(wd))
            out = ask("Output image path",
                      os.path.join(parent, f"{name}_modified.img"))
            method, read_only, extra_mb = _ask_repack_options()
            run_op(cmd_repack, wd, out, method, read_only, extra_mb)

        elif ch == "4":
            banner()
            d = ask("Directory containing .img files")
            if not d: continue
            out_base = ask("Output workspace base", WORK_DIR)
            run_op(cmd_batch_unpack, d, out_base)

        elif ch == "5":
            banner()
            ws = ask("Workspace directory", WORK_DIR)
            if not ws: continue
            out_dir = ask("Output directory for repacked images",
                          os.path.dirname(os.path.abspath(ws)))

            # Discover partitions
            partitions = []
            try:
                for entry in sorted(os.listdir(ws)):
                    full = os.path.join(ws, entry)
                    if os.path.isdir(full) and os.path.isdir(os.path.join(full,"META")):
                        partitions.append(entry)
            except Exception:
                pass

            if not partitions:
                err("No unpacked partitions found in that directory"); pause(); continue

            print(f"\n  {W}Found {len(partitions)} partitions:{N} {', '.join(partitions)}")
            use_same = ask("Use same options for all? (y/n)", "n").lower()

            per_opts = {}
            if use_same == "y":
                method, read_only, extra_mb = _ask_repack_options()
                for p in partitions:
                    per_opts[p] = (method, read_only, extra_mb)
            else:
                for p in partitions:
                    print(f"\n  {C}━━━ {p} ━━━{N}")
                    m, ro, ex = _ask_repack_options_for(p)
                    per_opts[p] = (m, ro, ex)

            run_op(cmd_batch_repack, ws, out_dir, per_opts)

        elif ch == "6":
            banner()
            img = ask("Image path")
            if not img: continue
            run_op(cmd_info, img)

        elif ch == "7":
            banner()
            s = ask("Sparse image path")
            if not s: continue
            r = ask("Output raw path", s.replace(".img", ".raw.img"))
            run_op(to_raw, s, r)

        elif ch == "8":
            banner()
            r = ask("Raw image path")
            if not r: continue
            s = ask("Output sparse path", r.replace(".raw.img", ".sparse.img"))
            run_op(to_sparse, r, s)

        elif ch == "0":
            break


# ─── SUPER MENU ───────────────────────────────────────────────────────────────
def menu_super():
    while True:
        banner()
        print(f"  {W}── Super.img Operations ──{N}\n")
        print("  [1]  Unpack super.img")
        print("  [2]  Repack super.img")
        print("  [3]  Full workflow  (unpack → edit partitions → repack)")
        print("  [0]  Back")

        ch = choose("Choice")

        if ch == "1":
            banner()
            img = ask("super.img path")
            if not img: continue
            out = ask("Output directory", os.path.join(WORK_DIR, "super_unpacked"))
            run_op(cmd_super_unpack, img, out)

        elif ch == "2":
            banner()
            pd  = ask("Partitions directory",
                      os.path.join(WORK_DIR, "super_unpacked", "partitions"))
            out = ask("Output super.img", os.path.join(WORK_DIR, "super_repacked.img"))
            md  = ask("META directory",
                      os.path.join(os.path.dirname(pd), "META"))
            run_op(cmd_super_repack, pd, out, md)

        elif ch == "3":
            banner()
            img = ask("super.img path")
            if not img: continue
            wd  = os.path.join(WORK_DIR, "super_unpacked")
            try:
                parts_dir = cmd_super_unpack(img, wd)
            except Exception as e:
                err(str(e)); pause(); continue

            print(f"\n  {Y}Run EXT4 Unpack/Edit on each .img in:{N}")
            print(f"  {C}{parts_dir}{N}")
            input(f"\n  {Y}Press Enter when all partitions are repacked to continue …{N}")
            out = ask("Output super.img", os.path.join(WORK_DIR, "super_modified.img"))
            run_op(cmd_super_repack, parts_dir, out, os.path.join(wd, "META"))

        elif ch == "0":
            break

# ─── MAIN MENU ────────────────────────────────────────────────────────────────
def main_menu():
    while True:
        banner()
        print(f"  {W}Main Menu{N}\n")
        print("  [1]  EXT4 Operations       (unpack / repack / edit)")
        print("  [2]  Super.img Operations  (unpack / repack)")
        print("  [3]  Setup  (install deps, prepare tools)")
        print("  [4]  Show workspace")
        print("  [5]  Fix permissions  (can't paste files? run this)")
        print("  [0]  Exit\n")

        ch = choose("Choice")

        if   ch == "1":  menu_ext4()
        elif ch == "2":  menu_super()
        elif ch == "3":  run_op(cmd_setup)
        elif ch == "4":
            banner()
            makedirs(WORK_DIR)
            print(f"\n  {W}Workspace:{N} {C}{WORK_DIR}{N}\n")
            try:
                entries = sorted(os.listdir(WORK_DIR))
                if entries:
                    for e in entries:
                        fp   = os.path.join(WORK_DIR, e)
                        kind = "DIR " if os.path.isdir(fp) else "FILE"
                        size = ""
                        if os.path.isfile(fp):
                            size = f"  {os.path.getsize(fp)//1024//1024} MB"
                        print(f"    {DIM}{kind}{N}  {e}{size}")
                else:
                    print("    (empty)")
            except Exception as e:
                print(f"    {R}{e}{N}")
            pause()
        elif ch == "5":
            banner()
            target = ask("Directory to fix", WORK_DIR)
            run_op(cmd_fix_perms, target)
        elif ch == "0":
            print(f"\n  {G}Goodbye!{N}\n")
            sys.exit(0)

# ─── CLI entry point ──────────────────────────────────────────────────────────
def usage():
    print(f"""
{W}Android ROM Image Tool v{VERSION}{N}

Usage: python3 rom_tool.py [command] [args]

Commands:
  setup
  unpack   <img> [name] [outdir]
  repack   <workdir> [out.img]
  info     <img>
  super-unpack  <super.img> [outdir]
  super-repack  <parts_dir> [out.img] [meta_dir]
  batch-unpack  <img_dir>
  fix-perms     [path]

  (no args)  →  interactive menu
""")

if __name__ == "__main__":
    # Ensure log file exists
    makedirs(os.path.dirname(LOG_FILE))
    open(LOG_FILE, "a").close()

    args = sys.argv[1:]

    if not args:
        try:
            main_menu()
        except KeyboardInterrupt:
            print(f"\n\n  {Y}Interrupted.{N}\n")
            sys.exit(0)

    elif args[0] == "setup":
        cmd_setup()
    elif args[0] == "unpack":
        cmd_unpack(*args[1:4])
    elif args[0] == "repack":
        cmd_repack(*args[1:3])
    elif args[0] == "info":
        cmd_info(args[1])
    elif args[0] == "super-unpack":
        cmd_super_unpack(*args[1:3])
    elif args[0] == "super-repack":
        cmd_super_repack(*args[1:4])
    elif args[0] == "batch-unpack":
        cmd_batch_unpack(*args[1:3])
    elif args[0] == "fix-perms":
        cmd_fix_perms(args[1] if len(args) > 1 else None)
    elif args[0] in ("help", "--help", "-h"):
        usage()
    else:
        usage()
        sys.exit(1)
