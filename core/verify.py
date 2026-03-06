"""
core/verify.py  —  Diff original vs repacked image (uid/gid/mode/SELinux)
"""

import os
import subprocess
import tempfile

from core.common import *
from core.image  import load_meta


def cmd_verify(work_dir):
    """
    Mount the original and repacked images side-by-side and diff every file's
    uid, gid, mode, and SELinux context.  Prints a summary table of differences.
    """
    meta_dir = os.path.join(work_dir, "META")
    if not os.path.isdir(meta_dir):
        raise FileNotFoundError(f"META/ not found in {work_dir}")

    info      = load_meta(meta_dir)
    part_name = info.get("partition_name", os.path.basename(work_dir))
    raw_img   = info.get("raw_img_path", "").strip()

    parent      = os.path.dirname(os.path.abspath(work_dir))
    repack_img  = os.path.join(parent, f"{part_name}_repacked.img")

    if not os.path.isfile(repack_img):
        warn(f"Repacked image not found at {repack_img}")
        repack_img = ask("Path to repacked .img")

    if not raw_img or not os.path.isfile(raw_img):
        warn(f"Original raw image not found: {raw_img}")
        raw_img = ask("Path to original raw .img")

    ok(f"Comparing: {os.path.basename(raw_img)}  vs  {os.path.basename(repack_img)}")

    def mount_and_read(img_path, label):
        mnt = tempfile.mkdtemp(prefix=f"verify_{label}_")
        try:
            r = subprocess.run(["sudo", "mount", "-o", "loop,ro", img_path, mnt],
                               stderr=subprocess.PIPE)
            if r.returncode != 0:
                warn(f"Cannot mount {img_path}: {r.stderr.decode(errors='replace').strip()}")
                return None, mnt
            data = {}
            for root, dirs, files in walk_real(mnt):
                for entry in [root] + [os.path.join(root, f) for f in files]:
                    rel = entry[len(mnt):] or "/"
                    try:
                        st   = os.lstat(entry)
                        uid  = st.st_uid
                        gid  = st.st_gid
                        mode = oct(st.st_mode)[-4:]
                    except Exception:
                        uid, gid, mode = "?", "?", "?"
                    ctx = ""
                    try:
                        r2 = subprocess.run(
                            ["getfattr", "-n", "security.selinux",
                             "--only-values", "--absolute-names", entry],
                            capture_output=True)
                        if r2.returncode == 0:
                            ctx = r2.stdout.decode(errors="replace").strip().rstrip("\x00")
                    except Exception:
                        pass
                    data[rel] = (uid, gid, mode, ctx)
            return data, mnt
        except Exception as e:
            warn(f"Error reading {label}: {e}")
            return None, mnt

    orig_data,   mnt_orig   = mount_and_read(raw_img,    "orig")
    repack_data, mnt_repack = mount_and_read(repack_img, "repacked")

    try:
        if orig_data is None or repack_data is None:
            warn("Could not read one or both images. Check sudo/mount permissions.")
            return

        orig_paths   = set(orig_data)
        repack_paths = set(repack_data)

        missing_in_r = sorted(orig_paths - repack_paths)
        extra_in_r   = sorted(repack_paths - orig_paths)
        diffs        = [(p, orig_data[p], repack_data[p])
                        for p in sorted(orig_paths & repack_paths)
                        if orig_data[p] != repack_data[p]]

        print()
        print(f"  {W}Verify Results: {part_name}{N}")
        print(f"  Original:  {len(orig_paths)} entries")
        print(f"  Repacked:  {len(repack_paths)} entries")
        print()

        if missing_in_r:
            print(f"  {R}MISSING from repacked ({len(missing_in_r)}):{N}")
            for p in missing_in_r[:30]:
                print(f"    {p}")
            if len(missing_in_r) > 30:
                print(f"    … and {len(missing_in_r)-30} more")
            print()

        if extra_in_r:
            print(f"  {Y}EXTRA in repacked ({len(extra_in_r)}):{N}")
            for p in extra_in_r[:10]:
                print(f"    {p}")
            if len(extra_in_r) > 10:
                print(f"    … and {len(extra_in_r)-10} more")
            print()

        if diffs:
            print(f"  {Y}CHANGED ({len(diffs)}):{N}")
            print(f"  {'PATH':<55}  {'ORIG uid:gid mode ctx':>32}  {'REPACK':>32}")
            for p, o, r in diffs[:50]:
                orig_s   = f"{o[0]}:{o[1]} {o[2]} {o[3][:15]}"
                repack_s = f"{r[0]}:{r[1]} {r[2]} {r[3][:15]}"
                print(f"  {p:<55}  {orig_s:>32}  {repack_s:>32}")
            if len(diffs) > 50:
                print(f"    … and {len(diffs)-50} more")
            print()

        if not missing_in_r and not diffs:
            ok("Images appear identical in structure, permissions, and SELinux!")
        else:
            warn(f"Found {len(missing_in_r) + len(diffs)} difference(s). "
                 "Missing files / wrong perms = bootloop cause.")

    finally:
        for mnt in [mnt_orig, mnt_repack]:
            subprocess.run(["sudo", "umount", "-l", mnt], stderr=subprocess.DEVNULL)
            try:
                os.rmdir(mnt)
            except Exception:
                pass
