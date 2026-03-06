"""
core/apex.py  —  APEX flattening and signature stripping
"""

import os
import shutil
import subprocess
import tempfile

from core.common   import *
from core.image    import load_meta
from core.fsconfig import save_file_list


def _apex_extract_payload(payload_img, dest_dir):
    """
    Extract apex_payload.img (ext4) into dest_dir.
    Strategy 1: debugfs rdump (no root)
    Strategy 2: sudo mount -o loop,ro
    """
    debugfs_bin = shutil.which("debugfs")
    if debugfs_bin:
        try:
            r = subprocess.run(
                [debugfs_bin, "-R", f"rdump / {dest_dir}", payload_img],
                stdout=subprocess.PIPE, stderr=subprocess.PIPE
            )
            if r.returncode == 0 or os.listdir(dest_dir):
                return "debugfs"
        except Exception:
            pass

    mnt_tmp = tempfile.mkdtemp(prefix="apex_mnt_")
    try:
        r = subprocess.run(
            ["sudo", "mount", "-o", "loop,ro", payload_img, mnt_tmp],
            stderr=subprocess.PIPE
        )
        if r.returncode == 0:
            subprocess.run(
                ["sudo", "cp", "-a", "--preserve=all",
                 mnt_tmp + "/.", dest_dir + "/"],
                check=True
            )
            return "mount"
    except Exception:
        pass
    finally:
        subprocess.run(["sudo", "umount", "-l", mnt_tmp], stderr=subprocess.DEVNULL)
        try:
            os.rmdir(mnt_tmp)
        except Exception:
            pass

    raise RuntimeError(
        "Cannot extract apex_payload.img — install debugfs (apt install e2tools) "
        "or run with sudo."
    )


def _flatten_one_apex(apex_path):
    """Flatten a single .apex or .capex to a directory. Returns dest_dir."""
    import zipfile as _zipfile

    base = apex_path
    for ext in (".capex", ".apex"):
        if base.endswith(ext):
            base = base[:-len(ext)]
            break
    dest_dir = base
    os.makedirs(dest_dir, exist_ok=True)

    with tempfile.TemporaryDirectory(prefix="apex_outer_") as outer_tmp:
        try:
            with _zipfile.ZipFile(apex_path) as z:
                z.extractall(outer_tmp)
        except Exception as e:
            raise RuntimeError(f"Cannot unzip {apex_path}: {e}")

        inner_apex = os.path.join(outer_tmp, "original_apex")
        if os.path.isfile(inner_apex):
            with tempfile.TemporaryDirectory(prefix="apex_inner_") as inner_tmp:
                with _zipfile.ZipFile(inner_apex) as z:
                    z.extractall(inner_tmp)
                for item in os.listdir(inner_tmp):
                    s = os.path.join(inner_tmp, item)
                    d = os.path.join(outer_tmp, item)
                    if not os.path.exists(d):
                        (shutil.copytree if os.path.isdir(s) else shutil.copy2)(s, d)
            os.remove(inner_apex)

        skip_items = {"apex_payload.img", "META-INF", "original_apex"}
        for item in os.listdir(outer_tmp):
            if item in skip_items:
                continue
            s = os.path.join(outer_tmp, item)
            d = os.path.join(dest_dir, item)
            if os.path.isdir(s):
                if not os.path.exists(d):
                    shutil.copytree(s, d)
            else:
                shutil.copy2(s, d)

        payload = os.path.join(outer_tmp, "apex_payload.img")
        if os.path.isfile(payload):
            _apex_extract_payload(payload, dest_dir)

    os.remove(apex_path)
    return dest_dir


def _strip_apex_signatures(apex_dir):
    """Remove META-INF/ + leftover .img blobs from a flattened APEX dir."""
    removed = []
    meta_inf = os.path.join(apex_dir, "META-INF")
    if os.path.isdir(meta_inf):
        shutil.rmtree(meta_inf)
        removed.append("META-INF/")
    for f in list(os.listdir(apex_dir)):
        fp = os.path.join(apex_dir, f)
        if f.endswith(".img") and os.path.isfile(fp):
            os.remove(fp)
            removed.append(f)
    return removed


def cmd_flatten_apexes(workspace_dir):
    """
    For every unpacked partition in workspace_dir:
      1. Extract remaining .apex / .capex containers → flat directories
      2. Strip META-INF/ + *.img from ALL APEX directories
      3. Refresh META/file_list.txt for the next repack
    """
    if not os.path.isdir(workspace_dir):
        raise FileNotFoundError(f"Workspace not found: {workspace_dir}")

    parts = []
    for entry in sorted(os.listdir(workspace_dir)):
        p     = os.path.join(workspace_dir, entry)
        meta  = os.path.join(p, "META")
        files = os.path.join(p, "files")
        if os.path.isdir(p) and os.path.isdir(meta) and os.path.isdir(files):
            parts.append((entry, p, files, meta))

    if not parts:
        warn("No unpacked partitions found in workspace (need META/ + files/).")
        return

    total_flattened = 0
    total_stripped  = 0
    total_failed    = 0

    for part_name, work_dir, files_dir, meta_dir in parts:
        header(f"Processing: {part_name}")

        apex_files = []
        for root, _dirs, fnames in os.walk(files_dir):
            for f in sorted(fnames):
                if f.endswith(".apex") or f.endswith(".capex"):
                    apex_files.append(os.path.join(root, f))

        if apex_files:
            log(f"Found {len(apex_files)} APEX container(s) to flatten")
            for apex_path in apex_files:
                rel = os.path.relpath(apex_path, files_dir)
                log(f"  Flattening: {rel}")
                try:
                    dest = _flatten_one_apex(apex_path)
                    ok(f"  → {os.path.relpath(dest, files_dir)}/")
                    total_flattened += 1
                except Exception as exc:
                    warn(f"  FAILED {rel}: {exc}")
                    total_failed += 1
        else:
            log("No .apex / .capex files (already flat or none present)")

        log("Scanning for signature artifacts in APEX directories …")
        for root, _dirs, fnames in os.walk(files_dir):
            if "apex_manifest.pb" in fnames or "apex_manifest.json" in fnames:
                rel     = os.path.relpath(root, files_dir)
                removed = _strip_apex_signatures(root)
                if removed:
                    log(f"  Stripped from {rel}/: {', '.join(removed)}")
                    total_stripped += len(removed)

        try:
            info       = load_meta(meta_dir)
            nested_sub = info.get("nested_subdir", "").strip()
            files_sub  = files_dir
            if nested_sub:
                c = os.path.join(files_dir, nested_sub)
                if os.path.isdir(c):
                    files_sub = c
            save_file_list(files_sub, meta_dir)
            ok(f"File list updated for {part_name}")
        except Exception as exc:
            warn(f"Could not update file list for {part_name}: {exc}")

    print()
    ok("═" * 50)
    ok("Flatten complete")
    print(f"  APEXes flattened       : {total_flattened}")
    print(f"  Signature sets stripped: {total_stripped}")
    if total_failed:
        warn(f"  Failures               : {total_failed}  (see log)")
    print()
    warn("Remember: add  ro.apex.updatable=false  to system/build.prop")
    warn("          and vendor/build.prop before flashing.")
    print()
