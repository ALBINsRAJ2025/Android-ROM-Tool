"""
core/setup.py  —  First-run setup and permission fix
"""

import os
import subprocess

from core.common import *


def cmd_setup():
    check_ubuntu()

    a        = arch()
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

    pkgs = ["e2fsprogs", "util-linux", "attr",
            "python3", "coreutils", "file", "bc", "pv"]
    log("Updating apt …")
    subprocess.run(["sudo", "apt-get", "update", "-qq"],
                   stdout=open(LOG_FILE, "a"), stderr=subprocess.STDOUT)
    log(f"Installing: {' '.join(pkgs)}")
    subprocess.run(["sudo", "apt-get", "install", "-y"] + pkgs,
                   stdout=open(LOG_FILE, "a"), stderr=subprocess.STDOUT)

    r = subprocess.run(["apt-cache", "show", "android-sdk-libsparse-utils"],
                       capture_output=True)
    if r.returncode == 0:
        subprocess.run(
            ["sudo", "apt-get", "install", "-y", "android-sdk-libsparse-utils"],
            stdout=open(LOG_FILE, "a"), stderr=subprocess.STDOUT
        )

    ok("Setup complete!")
    own(WORK_DIR)


def cmd_fix_perms(target=None):
    target = target or WORK_DIR
    makedirs(target)
    log(f"Fixing permissions on: {target}")
    own(target)
    ok(f"Done — {target} is now writable by {REAL_USER}")
