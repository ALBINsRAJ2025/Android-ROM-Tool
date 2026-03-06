"""
core/repack.py  —  EXT4 image repack engine
"""

import os
import shutil
import tempfile

from core.common  import *
from core.image   import to_sparse, load_meta
from core.fsconfig import (
    normalize_fsconfig_paths, prune_deleted_from_configs,
    update_configs_for_new_files, ensure_complete_fsconfig,
    repair_file_contexts, generate_explicit_file_contexts,
    _clean_filename,
)

REPACK_METHODS = {
    "1": "make_ext4fs   (recommended — Android-native, most compatible)",
    "2": "mke2fs -d     (populate directly, no e2fsdroid needed)",
    "3": "mke2fs + e2fsdroid  (two-step, legacy compat)",
}

# ─── image size calculation ───────────────────────────────────────────────────
def _calc_image_size(fs_dir, meta_dir, block_size, orig_size,
                     extra_mb=0, exclude_paths=None):
    """Return (new_size_bytes, block_count)."""
    meta_abs      = os.path.realpath(meta_dir)
    exclude_paths = set(exclude_paths or [])
    exclude_paths.add(meta_abs)

    total = 0
    for root, dirs, files in walk_real(fs_dir, skip_abs=exclude_paths):
        dirs[:] = [d for d in dirs
                   if os.path.realpath(os.path.join(root, d)) not in exclude_paths]
        for fname in files:
            fp = os.path.join(root, fname)
            if os.path.realpath(fp) in exclude_paths:
                continue
            try:
                total += os.lstat(fp).st_size
            except Exception:
                pass
        total += 4096   # dir inode overhead

    if total == 0:
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

# ─── build methods ────────────────────────────────────────────────────────────
def _repack_make_ext4fs(fs_dir, raw_out, mount_point, cfg_path, ctx_path,
                        label, block_size, inode_size, new_size, read_only=True):
    """Method 1: make_ext4fs — create + populate in one shot."""
    mk  = require_tool("make_ext4fs")
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
    if not read_only:
        cmd += ["-w"]
    cmd += [raw_out, fs_dir]
    run(cmd)

def _repack_mke2fs_d(fs_dir, raw_out, mount_point, cfg_path, ctx_path,
                     label, block_size, inode_size, block_count, read_only=True):
    """Method 2: mke2fs -d (populate directly)."""
    mke2fs = require_tool("mke2fs")
    disable = ["^metadata_csum", "^64bit", "^huge_file",
               "^metadata_csum_seed", "^orphan_file"]
    cmd = [mke2fs, "-t", "ext4", "-b", str(block_size), "-I", str(inode_size),
           "-m", "0", "-O", ",".join(disable),
           "-E", "lazy_itable_init=0,lazy_journal_init=0",
           "-d", fs_dir]
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
    disable   = ["^metadata_csum", "^64bit", "^huge_file",
                 "^metadata_csum_seed", "^orphan_file", "^dir_index"]
    mk_cmd = [mke2fs, "-t", "ext4", "-b", str(block_size), "-I", str(inode_size),
              "-m", "0", "-O", ",".join(disable),
              "-E", "lazy_itable_init=0,lazy_journal_init=0"]
    if label and label not in ("<none>", ""):
        mk_cmd += ["-L", label]
    mk_cmd += [raw_out, str(block_count)]
    run(mk_cmd)

    e2_cmd = [e2fsdroid, "-f", fs_dir, "-T", "0"]
    if os.path.isfile(cfg_path):
        e2_cmd += ["-C", cfg_path]
    if os.path.isfile(ctx_path):
        e2_cmd += ["-S", ctx_path]
    e2_cmd += ["-a", mount_point, raw_out]
    run(e2_cmd)

# ─── hardlink staging tree ────────────────────────────────────────────────────
def _hardlink_tree(src_dir, dst_dir):
    """Recursively hardlink src_dir → dst_dir with clean filenames."""
    os.makedirs(dst_dir, exist_ok=True)
    for item in os.listdir(src_dir):
        s          = os.path.join(src_dir, item)
        clean_item = _clean_filename(item)
        d          = os.path.join(dst_dir, clean_item)
        if os.path.islink(s):
            if not os.path.exists(d) and not os.path.islink(d):
                os.symlink(os.readlink(s), d)
        elif os.path.isdir(s):
            _hardlink_tree(s, d)
        else:
            if os.path.exists(d):
                continue   # clean-named stub already present
            try:
                os.link(s, d)
            except OSError:
                shutil.copy2(s, d)

# ─── main repack command ──────────────────────────────────────────────────────
def cmd_repack(work_dir, output_img=None, method="1",
               read_only=True, extra_mb=0):
    meta_dir = os.path.join(work_dir, "META")

    if not os.path.isdir(work_dir):
        raise FileNotFoundError(f"Work directory not found: {work_dir}")
    if not os.path.isdir(meta_dir):
        raise FileNotFoundError("META/ missing — was this unpacked by this tool?")

    info        = load_meta(meta_dir)
    part_name   = info.get("partition_name", "partition")
    mount_point = info.get("mount_point", "/")
    block_size  = int(info.get("block_size",  "4096"))
    inode_size  = int(info.get("inode_size",  "256"))
    was_sparse  = info.get("original_was_sparse", "0") == "1"
    label       = info.get("label", "")
    orig_size   = int(info.get("original_size", "0"))

    files_dir  = os.path.join(work_dir, "files")
    legacy_fs  = os.path.join(work_dir, "fs")
    nested_sub = info.get("nested_subdir", "").strip()

    if os.path.isdir(files_dir):
        if nested_sub and os.path.isdir(os.path.join(files_dir, nested_sub)):
            fs_dir = os.path.join(files_dir, nested_sub)
            log(f"New layout (nested): content at files/{nested_sub}/")
        else:
            fs_dir = files_dir
            log("New layout: content at files/")
    elif os.path.isdir(legacy_fs):
        fs_dir = legacy_fs
        log("Legacy layout: content at fs/")
    else:
        fs_dir = work_dir
        log("Old layout: content at work_dir root")

    part_prefix     = mount_point.lstrip("/") or part_name
    ext4_mountpoint = f"/{part_prefix}"
    log(f"fs_config prefix: '{part_prefix}'  →  -a {ext4_mountpoint}")

    if not output_img:
        parent     = os.path.dirname(os.path.abspath(work_dir))
        output_img = os.path.join(parent, f"{part_name}_repacked.img")

    makedirs(os.path.dirname(output_img))

    cfg_path = os.path.join(meta_dir, "fs_config.txt")
    ctx_path = os.path.join(meta_dir, "file_contexts.txt")

    # Step 0: clean dirty path components in existing fs_config
    log("Normalizing fs_config paths …")
    normalize_fsconfig_paths(meta_dir)

    # Step 1: prune deleted files
    log("Checking for deleted files …")
    prune_deleted_from_configs(fs_dir, meta_dir, mount_point)

    # Step 2: add new files
    log("Checking for new files …")
    update_configs_for_new_files(fs_dir, meta_dir, mount_point)

    # Build exclude set (raw images must not end up inside the new image)
    raw_img_path      = info.get("raw_img_path", "").strip()
    skip_from_staging = set()
    if raw_img_path:
        skip_from_staging.add(os.path.realpath(raw_img_path))
    for _f in os.listdir(fs_dir):
        if _f.endswith(".raw.img") or (_f.endswith(".img") and _f.startswith(part_name)):
            skip_from_staging.add(os.path.realpath(os.path.join(fs_dir, _f)))

    if skip_from_staging:
        log(f"Staging excludes: {', '.join(os.path.basename(p) for p in skip_from_staging)}")

    # Step 3: image size
    log(f"Calculating image size (extra: {extra_mb} MB) …")
    new_size, block_count = _calc_image_size(
        fs_dir, meta_dir, block_size, orig_size, extra_mb,
        exclude_paths=skip_from_staging
    )
    log(f"Image: {new_size // 1024 // 1024} MB  ({block_count} blocks × {block_size}B)")
    log(f"Mode: {'read-only' if read_only else 'read-write'}  Method: {REPACK_METHODS.get(method,'?')}")

    raw_out = (output_img.replace(".img", ".raw.img") if was_sparse else output_img)

    staging = tempfile.mkdtemp(prefix="rom_stage_")
    try:
        log("Creating staging dir …")
        meta_abs = os.path.realpath(meta_dir)
        for item in os.listdir(fs_dir):
            src      = os.path.join(fs_dir, item)
            src_real = os.path.realpath(src)
            if src_real == meta_abs or item == "META":
                continue
            if src_real in skip_from_staging:
                continue
            clean_item = _clean_filename(item)
            dst        = os.path.join(staging, clean_item)
            if os.path.isdir(src) and not os.path.islink(src):
                _hardlink_tree(src, dst)
            else:
                if os.path.exists(dst):
                    continue
                try:
                    os.link(src, dst)
                except OSError:
                    shutil.copy2(src, dst)

        ensure_complete_fsconfig(staging, cfg_path, ctx_path, ext4_mountpoint)
        repair_file_contexts(ctx_path)

        tmp_ctx = ctx_path + ".norm.tmp"
        generate_explicit_file_contexts(ctx_path, staging, ext4_mountpoint, tmp_ctx)
        build_ctx = tmp_ctx

        if method == "1":
            log("Building with make_ext4fs …")
            _repack_make_ext4fs(staging, raw_out, ext4_mountpoint, cfg_path, build_ctx,
                                label, block_size, inode_size, new_size, read_only)
        elif method == "2":
            log("Building with mke2fs -d …")
            _repack_mke2fs_d(staging, raw_out, ext4_mountpoint, cfg_path, build_ctx,
                             label, block_size, inode_size, block_count, read_only)
        elif method == "3":
            log("Building with mke2fs + e2fsdroid …")
            _repack_mke2fs_e2fsdroid(staging, raw_out, ext4_mountpoint, cfg_path, build_ctx,
                                     label, block_size, inode_size, block_count, read_only)
        else:
            raise ValueError(f"Unknown repack method: {method}")

    finally:
        for _p in [staging, ctx_path + ".norm.tmp"]:
            try:
                if os.path.isdir(_p):
                    shutil.rmtree(_p)
                elif os.path.isfile(_p):
                    os.remove(_p)
            except Exception:
                pass

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
