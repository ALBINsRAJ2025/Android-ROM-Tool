"""
core/fsconfig.py  —  fs_config + file_contexts management
──────────────────────────────────────────────────────────
Everything related to reading, writing, normalising, pruning,
and generating fs_config.txt and file_contexts.txt files.

  extract_fs_config()         — dump xattrs from a mounted image
  normalize_fsconfig_paths()  — strip garbage bytes from stored paths
  prune_deleted_from_configs()— remove entries for deleted files
  update_configs_for_new_files()— add entries for new files
  ensure_complete_fsconfig()  — stub-fill any staging paths not in config
  repair_file_contexts()      — deduplicate + fix malformed ctx lines
  generate_explicit_file_contexts() — per-file explicit ctx from staging
  default_perm()              — heuristic uid/gid/mode/ctx for new files
  save_file_list()            — snapshot file list to META/
"""

import os
import stat as _stat
import struct
import subprocess
import re

from core.common import *

# ─── SELinux helpers ──────────────────────────────────────────────────────────
def _escape_fc_path(path):
    """Escape all ERE metacharacters in a path for use in file_contexts."""
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

# ─── permission defaults for new files ───────────────────────────────────────
def default_perm(cfg_path, is_dir):
    """Return (uid, gid, mode_str, selinux_ctx) for a new/unknown file."""
    p   = cfg_path.lower()
    ctx = _default_selinux(cfg_path)
    uid, gid, mode = 0, 0, "0755" if is_dir else "0644"

    if is_dir:
        if "/app/" in p or "/priv-app/" in p:
            uid, gid = 1000, 1000
        return uid, gid, mode, ctx

    ext = os.path.splitext(cfg_path)[1].lower()
    if any(x in p for x in ["/bin/", "/xbin/", "/sbin/", "/vendor/bin/"]):
        uid, gid, mode = 0, 2000, "0755"
    elif any(x in p for x in ["/lib/", "/lib64/"]):
        uid, gid, mode = 0, 0, "0644"
    elif any(x in p for x in ["/app/", "/priv-app/"]):
        uid, gid, mode = 1000, 1000, "0644"
    elif any(x in p for x in ["/framework/", "/etc/", "/vendor/etc/"]):
        uid, gid, mode = 0, 0, "0644"

    if ext in (".sh", ".py", ".pl", ".rb"):
        mode = "0755"
    elif ext in (".so", ".apk", ".jar", ".apex"):
        mode = "0644"

    return uid, gid, mode, ctx

# ─── filename cleaning ────────────────────────────────────────────────────────
def _clean_filename(name):
    """
    Strip trailing garbage bytes (ord < 0x21 or > 0x7e) from a filename.

    Extracted ROM images sometimes contain filenames with trailing spaces,
    control characters, or high bytes.  These break make_ext4fs lookups
    because the staged file (clean name) can't be found in fs_config (dirty).
    """
    result = name
    while result and (ord(result[-1]) < 0x21 or ord(result[-1]) > 0x7e):
        result = result[:-1]
    return result if result else name

# ─── fs_config extraction (from mounted image) ───────────────────────────────
def extract_fs_config(mount_dir, meta_dir, mount_point):
    """Walk a mounted image and dump uid/gid/mode/caps/SELinux to META/."""
    cfg_path = os.path.join(meta_dir, "fs_config.txt")
    ctx_path = os.path.join(meta_dir, "file_contexts.txt")

    log("Extracting fs_config and SELinux contexts …")

    cfg_lines = []
    ctx_lines = []

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
            st   = os.lstat(fpath)
            uid  = st.st_uid
            gid  = st.st_gid
            mode = oct(st.st_mode)[-4:]
        except Exception:
            uid, gid, mode = 0, 0, "0644"

        # Parse vfs_cap_data struct — do NOT just hex-encode raw bytes
        caps = "0"
        try:
            r = subprocess.run(
                ["getfattr", "-n", "security.capability",
                 "--only-values", "--absolute-names", fpath],
                capture_output=True
            )
            if r.returncode == 0 and r.stdout:
                raw = r.stdout
                if len(raw) >= 8:
                    permitted_lo = struct.unpack_from("<I", raw, 4)[0]
                    permitted_hi = struct.unpack_from("<I", raw, 12)[0] if len(raw) >= 16 else 0
                    cap_val = permitted_lo | (permitted_hi << 32)
                    if cap_val:
                        caps = hex(cap_val)
        except Exception:
            pass

        cfg_path_entry = part_prefix if rel == "/" else part_prefix + rel
        cfg_lines.append(f"{cfg_path_entry} {uid} {gid} {mode} {caps}")

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

        ctx_rel = ("/"+part_prefix) if rel == "/" else ("/"+part_prefix+rel)
        ctx_rel_esc = _escape_fc_path(ctx_rel)

        try:
            m2 = os.lstat(fpath).st_mode
            if   _stat.S_ISDIR(m2):   ftype = "-d"
            elif _stat.S_ISLNK(m2):   ftype = "-l"
            elif _stat.S_ISFIFO(m2):  ftype = "-p"
            elif _stat.S_ISSOCK(m2):  ftype = "-s"
            elif _stat.S_ISCHR(m2):   ftype = "-c"
            elif _stat.S_ISBLK(m2):   ftype = "-b"
            else:                     ftype = "--"
        except Exception:
            ftype = "--"

        ctx_lines.append(f"{ctx_rel_esc}    {ftype}    {ctx}")

    with open(cfg_path, "w") as f:
        f.write("\n".join(cfg_lines) + "\n")
    with open(ctx_path, "w") as f:
        f.write("\n".join(ctx_lines) + "\n")

    ok(f"fs_config:      {len(cfg_lines)} entries")
    ok(f"file_contexts:  {len(ctx_lines)} entries")

# ─── normalize (strip garbage bytes from stored paths) ───────────────────────
def normalize_fsconfig_paths(meta_dir):
    """
    Rewrite META/fs_config.txt so every path component is clean.

    Dirty names (trailing spaces/control chars) from imperfect inode reads
    end up verbatim in fs_config at unpack time.  _hardlink_tree() later
    stages files under clean names, causing make_ext4fs lookup failures.
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

        parts = stripped.split(None, 4)
        if not parts:
            clean_lines.append(line)
            continue

        orig_path    = parts[0]
        clean_path   = "/".join(_clean_filename(c) for c in orig_path.split("/"))

        if clean_path != orig_path:
            fixed += 1
            parts[0] = clean_path

        if clean_path in seen_keys:
            continue
        seen_keys.add(clean_path)
        clean_lines.append(" ".join(parts) + "\n")

    if fixed:
        with open(cfg_path, "w") as f:
            f.writelines(clean_lines)
        ok(f"Normalized fs_config: {fixed} dirty path(s) cleaned, {len(clean_lines)} entries total")
    else:
        log("fs_config paths already clean")

# ─── prune deleted entries ────────────────────────────────────────────────────
def prune_deleted_from_configs(fs_dir, meta_dir, mount_point):
    """Remove fs_config + file_contexts entries for files that no longer exist."""
    cfg_path    = os.path.join(meta_dir, "fs_config.txt")
    ctx_path    = os.path.join(meta_dir, "file_contexts.txt")
    part_prefix = mount_point.lstrip("/") or "system"

    # Add BOTH dirty (raw) and clean keys — normalize_fsconfig_paths has already
    # cleaned fs_config, so we must not prune those freshly-cleaned entries.
    existing = set()
    meta_abs = os.path.realpath(meta_dir)
    for root, dirs, files in walk_real(fs_dir, skip_abs={meta_abs}):
        rel     = root[len(fs_dir):]
        cfg_key = (part_prefix + rel).lstrip("/")
        existing.add(cfg_key)
        # Also add clean version of dir path
        clean_rel     = "/" + "/".join(_clean_filename(p) for p in rel.strip("/").split("/")) if rel.strip("/") else ""
        existing.add((part_prefix + clean_rel).lstrip("/"))
        for fname in files:
            fp   = os.path.join(root, fname)
            frel = fp[len(fs_dir):]
            existing.add((part_prefix + frel).lstrip("/"))      # dirty key
            clean_fname = _clean_filename(fname)
            if clean_fname != fname:
                existing.add((part_prefix + rel + "/" + clean_fname).lstrip("/"))  # clean key

    pruned_cfg = 0
    pruned_ctx = 0

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

    OTHER_PARTS = {"vendor","product","system_ext","odm","oem",
                   "apex","data","cache","proc","sys","dev"}

    if os.path.isfile(ctx_path):
        kept = []
        with open(ctx_path) as f:
            for line in f:
                stripped = line.strip()
                if not stripped or stripped.startswith("#"):
                    kept.append(line)
                    continue
                ctx_path_raw = stripped.split()[0] if stripped.split() else ""
                bare = ctx_path_raw.lstrip("/").replace("\\.", ".").replace("\\[","[").replace("\\]","]")
                first_seg = bare.split("/")[0] if bare else ""
                if first_seg == part_prefix or first_seg in OTHER_PARTS:
                    chk = bare
                elif bare:
                    chk = part_prefix + "/" + bare
                else:
                    chk = part_prefix
                if chk in existing or chk == part_prefix:
                    kept.append(line)
                else:
                    pruned_ctx += 1
        with open(ctx_path, "w") as f:
            f.writelines(kept)

    if pruned_cfg or pruned_ctx:
        ok(f"Pruned {pruned_cfg} cfg + {pruned_ctx} ctx entries for deleted files")
    else:
        log("No deleted entries to prune")

# ─── add entries for new files ────────────────────────────────────────────────
def update_configs_for_new_files(fs_dir, meta_dir, mount_point):
    """Append fs_config + file_contexts entries for files added since unpack."""
    list_path   = os.path.join(meta_dir, "file_list.txt")
    cfg_path    = os.path.join(meta_dir, "fs_config.txt")
    ctx_path    = os.path.join(meta_dir, "file_contexts.txt")

    original = set()
    if os.path.isfile(list_path):
        with open(list_path) as f:
            original = {line.strip() for line in f if line.strip()}

    part_prefix      = mount_point.lstrip("/") or "system"
    new_count        = 0
    cfg_new          = []
    ctx_new          = []
    added_cfg_paths  = set()

    meta_abs = os.path.realpath(meta_dir)
    for root, dirs, files in walk_real(fs_dir, skip_abs={meta_abs}):
        rel = root[len(fs_dir):] or "/"

        if rel not in original and rel != "/":
            cfg_entry = part_prefix + rel
            uid, gid, mode, ctx = default_perm(cfg_entry, is_dir=True)
            cfg_new.append(f"{cfg_entry} {uid} {gid} {mode} 0")
            ctx_esc = _escape_fc_path(mount_point + rel)
            ctx_new.append(f"{ctx_esc}    -d    {ctx}")
            log(f"  [NEW DIR]  {cfg_entry}")
            new_count += 1

        for fname in sorted(files):
            fp   = os.path.join(root, fname)
            frel = fp[len(fs_dir):]
            if frel not in original:
                clean_fname = _clean_filename(fname)
                frel_cfg    = frel[:-len(fname)] + clean_fname if clean_fname != fname else frel
                if frel_cfg in added_cfg_paths:
                    continue
                added_cfg_paths.add(frel_cfg)
                cfg_entry = part_prefix + frel_cfg
                uid, gid, mode, ctx = default_perm(cfg_entry, is_dir=False)
                cfg_new.append(f"{cfg_entry} {uid} {gid} {mode} 0")
                try:
                    ftype = "-l" if _stat.S_ISLNK(os.lstat(fp).st_mode) else "--"
                except Exception:
                    ftype = "--"
                ctx_esc = _escape_fc_path(mount_point + frel_cfg)
                ctx_new.append(f"{ctx_esc}    {ftype}    {ctx}")
                log(f"  [NEW FILE] {cfg_entry}")
                new_count += 1

    if cfg_new:
        with open(cfg_path, "a") as f:
            f.write("\n".join(cfg_new) + "\n")
        with open(ctx_path, "a") as f:
            f.write("\n".join(ctx_new) + "\n")
        ok(f"Added {new_count} new entries to fs_config + file_contexts")
    else:
        log("No new files detected")

# ─── ensure complete fs_config (stub missing staging paths) ──────────────────
VALID_FTYPES = {"--", "-d", "-l", "-p", "-s", "-c", "-b"}

def ensure_complete_fsconfig(staging_dir, cfg_path, ctx_path, mount_point):
    """Add stub entries for any staging paths missing from fs_config."""
    part_prefix = mount_point.lstrip("/") or "system"

    known_cfg = set()
    known_ctx = set()

    if os.path.isfile(cfg_path):
        with open(cfg_path) as f:
            for line in f:
                p = line.split()
                if p:
                    known_cfg.add(p[0])

    if os.path.isfile(ctx_path):
        with open(ctx_path) as f:
            for line in f:
                p = line.split()
                if p:
                    known_ctx.add(p[0])

    cfg_new = []
    ctx_new = []

    for root, dirs, files in walk_real(staging_dir):
        rel     = root[len(staging_dir):]
        cfg_key = part_prefix + rel if rel else part_prefix
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
                    if   _stat.S_ISLNK(m):   ftype, mode = "-l", "0777"
                    elif _stat.S_ISDIR(m):   ftype, mode = "-d", "0755"
                    else:                    ftype, mode = "--", "0644"
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
    if ctx_new:
        with open(ctx_path, "a") as f:
            f.writelines(ctx_new)

# ─── repair file_contexts ─────────────────────────────────────────────────────
def repair_file_contexts(ctx_path):
    """Deduplicate + fix malformed lines in file_contexts.txt."""
    if not os.path.isfile(ctx_path):
        return

    seen  = set()
    kept  = []
    dupes = 0

    with open(ctx_path) as f:
        for line in f:
            stripped = line.strip()
            if not stripped or stripped.startswith("#"):
                kept.append(line)
                continue
            parts = stripped.split()
            if len(parts) < 2:
                continue
            key = parts[0]
            if key in seen:
                dupes += 1
                continue
            seen.add(key)
            # Ensure ftype field is present
            if len(parts) >= 3 and parts[1] in VALID_FTYPES:
                kept.append(line)
            elif len(parts) >= 2 and parts[1].startswith("u:"):
                kept.append(f"{parts[0]}    --    {parts[1]}\n")
            else:
                kept.append(line)

    with open(ctx_path, "w") as f:
        f.writelines(kept)

    if dupes:
        log(f"repair_file_contexts: removed {dupes} duplicate entries")

# ─── generate explicit per-file file_contexts ────────────────────────────────
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
    Walk staging_dir and write one explicit fs-path→label line per file.

    Loads stored patterns from ctx_path, matches each staged path, falls
    back to _default_selinux().  Writes to tmp_ctx_path (caller passes as
    build_ctx to the image builder).
    """
    import re as _re

    prefix = ext4_mountpoint
    part   = prefix.lstrip("/")
    OTHER_PARTS = {
        "vendor", "product", "system_ext", "odm", "oem",
        "apex", "data", "cache", "proc", "sys", "dev",
    }

    patterns = []
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

                norm    = _norm_ctx_path(raw_path, prefix, part, OTHER_PARTS)
                pat_str = norm + "$"
                try:
                    compiled = _re.compile(pat_str)
                    lit = _re.split(r'[.*()\\[\]?+]', norm)[0]
                    patterns.append((lit, compiled, ftype_p, label_p))
                except _re.error:
                    pass

    patterns.sort(key=lambda x: len(x[0]), reverse=True)

    def lookup_label(abs_path):
        for (_, pat_re, _pftype, plabel) in patterns:
            try:
                if pat_re.match(abs_path):
                    return plabel
            except Exception:
                continue
        return _default_selinux(abs_path)

    out_lines = []
    seen      = set()
    count     = 0

    for root, dirs, files in walk_real(staging_dir):
        rel      = root[len(staging_dir):]
        abs_path = (prefix + rel) if rel else prefix
        abs_esc  = _escape_fc_path(abs_path)

        if abs_path not in seen:
            lbl     = lookup_label(abs_path)
            pattern = (abs_esc + "/?") if not rel else abs_esc
            out_lines.append(f"{pattern}    -d    {lbl}\n")
            seen.add(abs_path)
            count += 1

        for fname in sorted(files):
            fp       = os.path.join(root, fname)
            frel     = fp[len(staging_dir):]
            abs_path = prefix + frel
            abs_esc  = _escape_fc_path(abs_path)

            if abs_path in seen:
                continue

            try:
                m = os.lstat(fp).st_mode
                if   _stat.S_ISLNK(m):    ftype_h = "-l"
                elif _stat.S_ISDIR(m):    ftype_h = "-d"
                elif _stat.S_ISFIFO(m):   ftype_h = "-p"
                elif _stat.S_ISSOCK(m):   ftype_h = "-s"
                elif _stat.S_ISCHR(m):    ftype_h = "-c"
                elif _stat.S_ISBLK(m):    ftype_h = "-b"
                else:                     ftype_h = "--"
            except Exception:
                ftype_h = "--"

            lbl = lookup_label(abs_path)
            out_lines.append(f"{abs_esc}    {ftype_h}    {lbl}\n")
            seen.add(abs_path)
            count += 1

    with open(tmp_ctx_path, "w") as fout:
        fout.writelines(out_lines)

    ok(f"Generated explicit file_contexts: {count} entries (prefix={prefix})")

# ─── file list snapshot ───────────────────────────────────────────────────────
def save_file_list(fs_dir, meta_dir):
    """Snapshot all current paths in fs_dir to META/file_list.txt."""
    list_path = os.path.join(meta_dir, "file_list.txt")
    entries   = []
    meta_abs  = os.path.realpath(meta_dir)
    for root, dirs, files in walk_real(fs_dir, skip_abs={meta_abs}):
        rel = root[len(fs_dir):]
        entries.append(rel or "/")
        for fname in files:
            entries.append(os.path.join(root, fname)[len(fs_dir):])
    with open(list_path, "w") as f:
        f.write("\n".join(entries) + "\n")
    ok(f"Saved file list: {len(entries)} entries")
