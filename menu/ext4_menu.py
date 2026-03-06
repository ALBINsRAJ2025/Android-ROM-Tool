"""
menu/ext4_menu.py  —  EXT4 operations submenu
"""

import os

from core.common import *
from core.image  import to_raw, to_sparse, load_meta
from core.unpack import cmd_unpack
from core.repack import cmd_repack, REPACK_METHODS
from core.batch  import cmd_batch_unpack, cmd_batch_repack, cmd_info
from core.verify import cmd_verify
from menu.helpers import banner, ask, pause, choose, run_op


def _ask_repack_options():
    print(f"\n  {W}── Repack Options ──{N}")
    print(f"  {DIM}Method:{N}")
    for k, v in REPACK_METHODS.items():
        print(f"    [{k}] {v}")
    method = ask("Method", "1")
    if method not in REPACK_METHODS:
        method = "1"
    rw_ans    = ask("Read-only image? (y/n)", "y").lower()
    read_only = rw_ans != "n"
    extra_s   = ask("Extra MB beyond auto-calculated size", "0")
    try:
        extra_mb = int(extra_s)
    except ValueError:
        extra_mb = 0
    return method, read_only, extra_mb


def _ask_repack_options_for(part_name, default_method="1",
                            default_ro=True, default_extra=0):
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
        print("  [9]  Verify repack  (diff original vs repacked image)")
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
            out    = ask("Output image path",
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
            meta    = load_meta(os.path.join(wd, "META"))
            nested  = meta.get("nested_subdir","").strip()
            content = os.path.join(wd, "files")
            show    = os.path.join(content, nested) if nested else content
            print(f"\n  {Y}Edit files in:{N}  {C}{show}{N}")
            input(f"  {Y}Press Enter when done to repack …{N}")
            parent = os.path.dirname(os.path.abspath(wd))
            out    = ask("Output image path", os.path.join(parent, f"{name}_modified.img"))
            method, read_only, extra_mb = _ask_repack_options()
            run_op(cmd_repack, wd, out, method, read_only, extra_mb)

        elif ch == "4":
            banner()
            d        = ask("Directory containing .img files")
            if not d: continue
            out_base = ask("Output workspace base", WORK_DIR)
            run_op(cmd_batch_unpack, d, out_base)

        elif ch == "5":
            banner()
            ws = ask("Workspace directory", WORK_DIR)
            if not ws: continue
            out_dir = ask("Output directory for repacked images",
                          os.path.dirname(os.path.abspath(ws)))

            partitions = []
            try:
                for entry in sorted(os.listdir(ws)):
                    full = os.path.join(ws, entry)
                    if os.path.isdir(full) and os.path.isdir(os.path.join(full,"META")):
                        partitions.append(entry)
            except Exception:
                pass

            if not partitions:
                err("No unpacked partitions found"); pause(); continue

            print(f"\n  {W}Found {len(partitions)} partition(s):{N} {', '.join(partitions)}")
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

        elif ch == "9":
            banner()
            print(f"  {W}Verify Repack{N}\n")
            print("  Mounts the original and repacked images side-by-side")
            print("  and diffs uid/gid/mode/SELinux context for every file.")
            print("  Requires: sudo mount, getfattr\n")
            wd = ask("Partition work directory", WORK_DIR)
            run_op(cmd_verify, wd)

        elif ch == "0":
            break
