"""
menu/helpers.py  —  Shared interactive UI primitives
"""

import os
import sys

from core.common import *


def banner():
    """Clear screen and print the branded header."""
    os.system("clear")
    print(f"""{C}{BOLD}
  ╔══════════════════════════════════════════════════════════════╗
  ║   ██████╗  ██████╗ ███╗   ███╗    ████████╗ ██████╗  ██████╗ ██╗     ║
  ║   ██╔══██╗██╔═══██╗████╗ ████║       ██╔══╝██╔═══██╗██╔═══██╗██║     ║
  ║   ██████╔╝██║   ██║██╔████╔██║       ██║   ██║   ██║██║   ██║██║     ║
  ║   ██╔══██╗██║   ██║██║╚██╔╝██║       ██║   ██║   ██║██║   ██║██║     ║
  ║   ██║  ██║╚██████╔╝██║ ╚═╝ ██║       ██║   ╚██████╔╝╚██████╔╝███████╗║
  ╠══════════════════════════════════════════════════════════════╣
  ║     Android ROM Image Tool  ·  v{VERSION:<6}  ·  by ALBINsRAJ2025    ║
  ║     ext4 · sparse · super.img · fs_config · SELinux          ║
  ╚══════════════════════════════════════════════════════════════╝{N}
""")
    print(f"  {DIM}Session user: {W}{REAL_USER}{DIM}  (uid={REAL_UID} gid={REAL_GID}){N}\n")


def ask(prompt_text, default=""):
    """Prompt user for input, return answer (stripped). Returns default on blank."""
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
    """Read a single-character menu choice."""
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
