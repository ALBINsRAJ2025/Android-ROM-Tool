#!/usr/bin/env python3
"""
rom_tool.py  —  Android ROM Image Tool
───────────────────────────────────────
Launcher — parses CLI args or drops into the interactive menu.

All logic lives in the core/ and menu/ packages.

Created & owned by  ALBINsRAJ2025
"""

import os
import sys

# ── make sure our packages are on the path even when invoked from elsewhere ──
_HERE = os.path.dirname(os.path.realpath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

from core.common  import *
from core.image   import to_raw, to_sparse
from core.unpack  import cmd_unpack
from core.repack  import cmd_repack
from core.batch   import cmd_batch_unpack, cmd_batch_repack, cmd_info
from core.super   import cmd_super_unpack, cmd_super_repack
from core.setup   import cmd_setup, cmd_fix_perms
from core.apex    import cmd_flatten_apexes
from core.verify  import cmd_verify
from menu.main_menu import main_menu


def usage():
    print(f"""
{W}Android ROM Image Tool  v{VERSION}{N}
{DIM}Created & owned by  ALBINsRAJ2025{N}

Usage:  python3 rom_tool.py [command] [args]

Commands:
  setup
  unpack          <img> [name] [outdir]
  repack          <workdir> [out.img]
  info            <img>
  super-unpack    <super.img> [outdir]
  super-repack    <parts_dir> [out.img] [meta_dir]
  batch-unpack    <img_dir>
  batch-repack    <workspace_dir>
  flatten-apexes  [workspace]
  fix-perms       [path]
  verify          <workdir>

  (no args)  →  interactive menu
""")


if __name__ == "__main__":
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
    elif args[0] == "batch-repack":
        cmd_batch_repack(args[1] if len(args) > 1 else WORK_DIR)
    elif args[0] == "flatten-apexes":
        cmd_flatten_apexes(args[1] if len(args) > 1 else WORK_DIR)
    elif args[0] == "fix-perms":
        cmd_fix_perms(args[1] if len(args) > 1 else None)
    elif args[0] == "verify":
        cmd_verify(args[1] if len(args) > 1 else WORK_DIR)
    elif args[0] in ("help", "--help", "-h"):
        usage()
    else:
        usage()
        sys.exit(1)
