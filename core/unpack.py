"""
core/unpack.py  —  EXT4 image unpack
"""

import os
import subprocess
import tempfile

from core.common import *
from core.image  import detect_format, to_raw, save_image_meta, set_meta
from core.fsconfig import extract_fs_config, save_file_list


def _detect_nested_content(files_dir, part_name):
    """
    Detect system/system/ nested layout.
    Returns (content_dir, nested_subdir_name_or_None).
    """
    try:
        entries = [e for e in os.listdir(files_dir) if not e.startswith(".")]
        if len(entries) == 1 and entries[0] == part_name:
            candidate = os.path.join(files_dir, entries[0])
            if os.path.isdir(candidate) and not os.path.islink(candidate):
                if len(os.listdir(candidate)) > 3:
                    return candidate, entries[0]
    except Exception:
        pass
    return files_dir, None


def cmd_unpack(img_path, part_name=None, out_dir=None):
    if not os.path.isfile(img_path):
        raise FileNotFoundError(f"Image not found: {img_path}")

    if not part_name:
        part_name = os.path.basename(img_path).replace(".img", "")
    if not out_dir:
        out_dir = os.path.join(WORK_DIR, part_name)

    meta_dir  = os.path.join(out_dir, "META")
    files_dir = os.path.join(out_dir, "files")

    makedirs(out_dir)
    makedirs(meta_dir)
    makedirs(files_dir)

    fmt = detect_format(img_path)
    log(f"Detected format: {fmt}")

    if fmt == "super":
        raise RuntimeError("This looks like a super.img — use Super Unpack instead.")

    was_sparse = False
    raw_img    = os.path.join(out_dir, f"{part_name}.raw.img")

    if fmt == "sparse":
        was_sparse = True
        to_raw(img_path, raw_img)
    else:
        raw_img = img_path

    log("Running e2fsck …")
    subprocess.run(["sudo", "e2fsck", "-fy", raw_img],
                   stdout=open(LOG_FILE, "a"), stderr=subprocess.STDOUT)

    info = save_image_meta(raw_img, meta_dir, part_name)
    set_meta(meta_dir, "raw_img_path", raw_img)
    if was_sparse:
        set_meta(meta_dir, "original_was_sparse", "1")

    mount_point = info["mount_point"]

    mnt_tmp = tempfile.mkdtemp(prefix="rom_mnt_")
    try:
        log(f"Mounting {raw_img} …")
        sudo_run(["mount", "-o", "loop,ro", raw_img, mnt_tmp])

        log("Copying files → files/ …")
        r = subprocess.run(
            ["sudo", "cp", "-a", "--preserve=all", mnt_tmp + "/.", files_dir + "/"],
            stderr=subprocess.PIPE
        )
        if r.returncode != 0:
            warn("cp failed, trying rsync …")
            subprocess.run(
                ["sudo", "rsync", "-aAX", mnt_tmp + "/", files_dir + "/"],
                check=True
            )

        extract_fs_config(mnt_tmp, meta_dir, mount_point)

    finally:
        subprocess.run(["sudo", "umount", "-l", mnt_tmp], stderr=subprocess.DEVNULL)
        try:
            os.rmdir(mnt_tmp)
        except Exception:
            pass

    content_dir, nested = _detect_nested_content(files_dir, part_name)
    if nested:
        set_meta(meta_dir, "nested_subdir", nested)
        log(f"Nested layout detected: files/{nested}/")
    else:
        set_meta(meta_dir, "nested_subdir", "")

    own(files_dir)
    own(meta_dir)
    own(out_dir)

    save_file_list(content_dir, meta_dir)

    ok("═" * 50)
    ok(f"Unpack complete: {part_name}")
    print(f"\n  {W}Work dir  :{N} {out_dir}")
    print(f"  {W}Metadata  :{N} {meta_dir}")
    print(f"  {W}Content   :{N} {content_dir}")
    if nested:
        print(f"  {Y}⚠ Nested layout — edit files inside:{N}")
    print(f"\n  {Y}Edit files inside:{N}")
    print(f"  {C}{content_dir}{N}\n")
