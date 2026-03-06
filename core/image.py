"""
core/image.py  —  Image format detection, sparse/raw conversion, metadata
"""

import os
import struct
import subprocess
import shutil
import re

from core.common import *

# ─── known partition mount points ─────────────────────────────────────────────
KNOWN_MOUNT_POINTS = {
    "system":      "/",
    "system_ext":  "/system_ext",
    "product":     "/product",
    "vendor":      "/vendor",
    "odm":         "/odm",
    "vendor_dlkm": "/vendor_dlkm",
    "odm_dlkm":    "/odm_dlkm",
    "system_dlkm": "/system_dlkm",
}

# ─── format detection ─────────────────────────────────────────────────────────
def detect_format(img):
    """Return 'sparse', 'ext4', 'super', or 'unknown'."""
    try:
        with open(img, "rb") as f:
            header = f.read(4096)
    except Exception:
        return "unknown"

    if len(header) >= 4 and struct.unpack_from("<I", header, 0)[0] == 0xED26FF3A:
        return "sparse"

    if len(header) >= 1082:
        if struct.unpack_from("<H", header, 1080)[0] == 0xEF53:
            return "ext4"

    if len(header) >= 4096 + 4:
        if struct.unpack_from("<I", header, 4096)[0] == 0x4D0CC467:
            return "super"

    try:
        with open(img, "rb") as f:
            f.seek(1024)
            sb = f.read(2)
        if len(sb) == 2 and struct.unpack("<H", sb)[0] == 0xEF53:
            return "ext4"
    except Exception:
        pass

    return "unknown"

# ─── sparse ↔ raw ─────────────────────────────────────────────────────────────
def to_raw(src, dst):
    fmt = detect_format(src)
    if fmt == "sparse":
        log("Converting sparse → raw …")
        run([require_tool("simg2img"), src, dst])
    else:
        log("Already raw — copying …")
        shutil.copy2(src, dst)

def to_sparse(src, dst):
    log("Converting raw → sparse …")
    run([require_tool("img2simg"), src, dst])

# ─── metadata save / load ─────────────────────────────────────────────────────
def save_image_meta(raw_img, meta_dir, part_name):
    makedirs(meta_dir)
    dump_path = os.path.join(meta_dir, "dumpe2fs.txt")

    try:
        r = run(["dumpe2fs", "-h", raw_img], capture=True, check=False)
        dump = r.stdout.decode(errors="replace")
        with open(dump_path, "w") as f:
            f.write(dump)
    except Exception:
        dump = ""

    def extract(pattern):
        m = re.search(pattern, dump, re.MULTILINE)
        return m.group(1).strip() if m else ""

    block_size  = extract(r"^Block size:\s+(\S+)")
    block_count = extract(r"^Block count:\s+(\S+)")
    inode_count = extract(r"^Inode count:\s+(\S+)")
    inode_size  = extract(r"^Inode size:\s+(\S+)")
    mount_point = extract(r"^Last mounted on:\s+(\S+)")
    label       = extract(r"^Filesystem volume name:\s+(\S+)")

    if not mount_point or mount_point.startswith("<"):
        mount_point = KNOWN_MOUNT_POINTS.get(part_name, f"/{part_name}")

    img_size = os.path.getsize(raw_img)

    info = {
        "partition_name":      part_name,
        "mount_point":         mount_point,
        "block_size":          block_size  or "4096",
        "block_count":         block_count or "0",
        "inode_count":         inode_count or "0",
        "inode_size":          inode_size  or "256",
        "label":               label       or "",
        "original_size":       str(img_size),
        "original_was_sparse": "0",
    }

    info_path = os.path.join(meta_dir, "image_info.txt")
    with open(info_path, "w") as f:
        for k, v in info.items():
            f.write(f"{k}={v}\n")

    log(f"Saved metadata → {info_path}")
    return info

def load_meta(meta_dir):
    info_path = os.path.join(meta_dir, "image_info.txt")
    if not os.path.isfile(info_path):
        raise FileNotFoundError(f"META not found: {info_path}")
    info = {}
    with open(info_path) as f:
        for line in f:
            line = line.strip()
            if "=" in line:
                k, v = line.split("=", 1)
                info[k.strip()] = v.strip()
    return info

def set_meta(meta_dir, key, value):
    info_path = os.path.join(meta_dir, "image_info.txt")
    lines = []
    found = False
    if os.path.isfile(info_path):
        with open(info_path) as f:
            for line in f:
                if line.strip().startswith(key + "="):
                    lines.append(f"{key}={value}\n")
                    found = True
                else:
                    lines.append(line)
    if not found:
        lines.append(f"{key}={value}\n")
    with open(info_path, "w") as f:
        f.writelines(lines)
