"""
menu/super_menu.py  —  super.img operations submenu
"""

import os

from core.common import *
from core.super  import cmd_super_unpack, cmd_super_repack
from menu.helpers import banner, ask, pause, choose, run_op


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
            md  = ask("META directory", os.path.join(os.path.dirname(pd), "META"))
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
            input(f"\n  {Y}Press Enter when all partitions are repacked …{N}")
            out = ask("Output super.img", os.path.join(WORK_DIR, "super_modified.img"))
            run_op(cmd_super_repack, parts_dir, out, os.path.join(wd, "META"))

        elif ch == "0":
            break
