"""
menu/main_menu.py  —  Top-level main menu
"""

import os
import sys

from core.common  import *
from core.setup   import cmd_setup, cmd_fix_perms
from core.apex    import cmd_flatten_apexes
from menu.helpers import banner, ask, pause, choose, run_op
from menu.ext4_menu   import menu_ext4
from menu.super_menu  import menu_super


def main_menu():
    while True:
        banner()
        print(f"  {W}Main Menu{N}\n")
        print("  [1]  EXT4 Operations       (unpack / repack / edit)")
        print("  [2]  Super.img Operations  (unpack / repack)")
        print("  [3]  Setup  (install deps, prepare tools)")
        print("  [4]  Show workspace")
        print("  [5]  Fix permissions  (can't paste files? run this)")
        print("  [6]  Flatten APEXes   (extract .apex/.capex → dirs, strip sigs)")
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
                        size = f"  {os.path.getsize(fp)//1024//1024} MB" if os.path.isfile(fp) else ""
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

        elif ch == "6":
            banner()
            print(f"  {W}Flatten APEXes{N}\n")
            print("  Scans every unpacked partition in your workspace, then:")
            print("    • Extracts any remaining .apex / .capex files → flat directories")
            print("    • Strips META-INF/ + leftover .img blobs from ALL apex dirs")
            print("    • Updates the saved file list so the next repack picks up changes")
            print()
            ws = ask("Workspace directory", WORK_DIR)
            run_op(cmd_flatten_apexes, ws)

        elif ch == "0":
            print(f"\n  {G}Goodbye!{N}\n")
            sys.exit(0)
