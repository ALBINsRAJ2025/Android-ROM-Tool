"""
core/batch.py  —  Batch unpack / repack + image info
"""

import os
import subprocess

from core.common import *
from core.image  import detect_format
from core.unpack import cmd_unpack
from core.repack import cmd_repack


def cmd_batch_unpack(img_dir, out_base=None):
    if not out_base:
        out_base = WORK_DIR

    known    = ["system","system_ext","product","vendor","odm","vendor_dlkm","odm_dlkm"]
    suffixes = ["", "_a", "_b"]
    found    = []

    for name in known:
        for sfx in suffixes:
            candidate = os.path.join(img_dir, f"{name}{sfx}.img")
            if os.path.isfile(candidate):
                found.append((candidate, name, os.path.join(out_base, name)))

    if not found:
        raise FileNotFoundError(f"No partition images found in: {img_dir}")

    log(f"Found {len(found)} partition image(s)")
    for img_path, pname, outdir in found:
        print(f"\n  {C}━━━ {pname} ━━━{N}")
        try:
            cmd_unpack(img_path, pname, outdir)
        except Exception as e:
            err(f"Failed to unpack {pname}: {e}")

    ok(f"Batch unpack complete: {len(found)} images")


def cmd_batch_repack(workspace_dir, out_dir=None, per_partition_opts=None):
    """
    Repack all unpacked partitions found in workspace_dir.

    per_partition_opts: dict {part_name: (method, read_only, extra_mb)}
    """
    if not out_dir:
        out_dir = os.path.dirname(os.path.abspath(workspace_dir))

    per_partition_opts = per_partition_opts or {}
    makedirs(out_dir)

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

    log(f"Found {len(candidates)} partition(s): {[n for n,_ in candidates]}")

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

    n_ok  = sum(1 for _, o, _ in results if o)
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
        r = subprocess.run(["dumpe2fs", "-h", img_path],
                           capture_output=True, text=True)
        if r.returncode == 0:
            interesting = ["Block size", "Block count", "Inode count",
                           "Volume name", "Last mounted",
                           "Filesystem features", "Filesystem UUID"]
            print()
            for line in r.stdout.splitlines():
                for key in interesting:
                    if line.startswith(key + ":"):
                        print(f"  {DIM}{line}{N}")
    elif fmt == "sparse":
        print(f"\n  {Y}(Sparse — convert to raw to read ext4 metadata){N}")
    elif fmt == "super":
        print(f"\n  {Y}(super.img — use Super Unpack to extract partitions){N}")
    print()
