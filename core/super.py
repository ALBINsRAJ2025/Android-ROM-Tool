"""
core/super.py  —  super.img unpack / repack + Python LP fallback extractor
"""

import os
import struct
import subprocess
import shutil
import tempfile
import glob

from core.common import *
from core.image  import detect_format, to_raw


# ─── LP metadata constants ────────────────────────────────────────────────────
LP_METADATA_HEADER_MAGIC = 0x4D0CC467
LP_SECTOR_SIZE           = 512
HEADER_SIZE              = 80
PARTITION_SIZE           = 52
EXTENT_SIZE              = 20


# ─── Python LP unpacker (fallback when lpunpack / imgkit are unavailable) ─────
def lpunpack_py(super_path, out_dir):
    os.makedirs(out_dir, exist_ok=True)
    log(f"LP Python extractor: scanning {super_path} …")

    with open(super_path, "rb") as f:
        scan_data = f.read(min(os.path.getsize(super_path), 128 * 1024 * 1024))

    meta_off = -1
    for offset in [4096, 8192, LP_SECTOR_SIZE, LP_SECTOR_SIZE * 2]:
        if offset + 4 <= len(scan_data):
            if struct.unpack_from("<I", scan_data, offset)[0] == LP_METADATA_HEADER_MAGIC:
                meta_off = offset
                break

    if meta_off < 0:
        for off in range(0, min(len(scan_data), 0x200000), LP_SECTOR_SIZE):
            if off + 4 <= len(scan_data):
                if struct.unpack_from("<I", scan_data, off)[0] == LP_METADATA_HEADER_MAGIC:
                    meta_off = off
                    break

    if meta_off < 0:
        raise RuntimeError("LP metadata not found in super image")

    log(f"LP metadata at offset 0x{meta_off:x}")

    try:
        h = struct.unpack_from(
            "<IHH I 32s I 32s I 32s I I I I I I I Q I",
            scan_data, meta_off
        )
    except struct.error as e:
        raise RuntimeError(f"Failed to parse LP header: {e}")

    header = {
        "header_size":       h[3],
        "partitions_offset": h[9],
        "partitions_count":  h[10],
        "extents_offset":    h[11],
        "extents_count":     h[12],
    }

    tables_off = meta_off + header["header_size"]

    partitions = []
    base = tables_off + header["partitions_offset"]
    for i in range(header["partitions_count"]):
        off = base + i * PARTITION_SIZE
        if off + PARTITION_SIZE > len(scan_data):
            break
        name_b, attrs, first_ext, num_ext = struct.unpack_from("<36sIII", scan_data, off)
        name = name_b.rstrip(b"\x00").decode("utf-8", errors="replace")
        if name:
            partitions.append({"name": name, "first_extent_index": first_ext,
                                "num_extents": num_ext})

    extents = []
    base = tables_off + header["extents_offset"]
    for i in range(header["extents_count"]):
        off = base + i * EXTENT_SIZE
        if off + EXTENT_SIZE > len(scan_data):
            break
        num_sectors, target_type, target_data = struct.unpack_from("<QII", scan_data, off)
        extents.append({"num_sectors": num_sectors, "target_data": target_data})

    log(f"Found {len(partitions)} partitions, {len(extents)} extents")

    with open(super_path, "rb") as sf:
        for part in partitions:
            name   = part["name"]
            p_exts = extents[part["first_extent_index"]:
                             part["first_extent_index"] + part["num_extents"]]
            out_path = os.path.join(out_dir, name + ".img")
            log(f"  Extracting: {name} → {out_path}")
            with open(out_path, "wb") as of:
                for ext in p_exts:
                    offset    = ext["target_data"] * LP_SECTOR_SIZE
                    length    = ext["num_sectors"] * LP_SECTOR_SIZE
                    remaining = length
                    sf.seek(offset)
                    while remaining > 0:
                        chunk = min(remaining, 1024 * 1024)
                        buf   = sf.read(chunk)
                        of.write(buf if buf else b"\x00" * chunk)
                        remaining -= chunk

    ok(f"LP extraction done → {out_dir}")


# ─── super unpack ─────────────────────────────────────────────────────────────
def cmd_super_unpack(super_img, out_dir=None):
    if not os.path.isfile(super_img):
        raise FileNotFoundError(f"super.img not found: {super_img}")

    if not out_dir:
        out_dir = os.path.join(WORK_DIR, "super_unpacked")

    meta_dir  = os.path.join(out_dir, "META")
    parts_dir = os.path.join(out_dir, "partitions")
    makedirs(meta_dir)
    makedirs(parts_dir)

    fmt        = detect_format(super_img)
    was_sparse = False
    raw_super  = os.path.join(out_dir, "super.raw.img")

    if fmt == "sparse":
        was_sparse = True
        to_raw(super_img, raw_super)
    else:
        raw_super = super_img

    raw_size = os.path.getsize(raw_super)
    with open(os.path.join(meta_dir, "super_info.txt"), "w") as f:
        f.write(f"original_super={super_img}\n")
        f.write(f"was_sparse={1 if was_sparse else 0}\n")
        f.write(f"raw_super_size={raw_size}\n")

    log("Unpacking LP partitions …")
    unpacked = False

    if not unpacked and shutil.which("lpunpack"):
        r = subprocess.run(["lpunpack", raw_super, parts_dir],
                           stdout=open(LOG_FILE, "a"), stderr=subprocess.STDOUT)
        unpacked = r.returncode == 0

    imgkit = tool("imgkit")
    if not unpacked and imgkit:
        r = subprocess.run([imgkit, "lpunpack", raw_super, parts_dir],
                           stdout=open(LOG_FILE, "a"), stderr=subprocess.STDOUT)
        unpacked = r.returncode == 0

    if not unpacked:
        log("Using built-in Python LP extractor …")
        lpunpack_py(raw_super, parts_dir)

    own(out_dir)

    imgs = sorted(glob.glob(os.path.join(parts_dir, "*.img")))
    ok("═" * 50)
    ok("Super unpack complete")
    print(f"\n  {W}Partitions:{N}")
    for img in imgs:
        mb = os.path.getsize(img) // 1024 // 1024
        print(f"    {G}✓{N}  {os.path.basename(img):<30} {mb} MB")
    print(f"\n  {W}Next:{N} Use EXT4 Unpack on each .img in:")
    print(f"  {C}{parts_dir}{N}\n")
    return parts_dir


# ─── super repack ─────────────────────────────────────────────────────────────
def cmd_super_repack(parts_dir, output_img=None, meta_dir=None):
    if not os.path.isdir(parts_dir):
        raise FileNotFoundError(f"Partitions directory not found: {parts_dir}")

    if not output_img:
        output_img = os.path.join(WORK_DIR, "super_repacked.img")
    if not meta_dir:
        meta_dir = os.path.join(os.path.dirname(parts_dir), "META")

    makedirs(os.path.dirname(output_img))

    was_sparse = False
    super_size = 0
    info_file  = os.path.join(meta_dir, "super_info.txt")
    if os.path.isfile(info_file):
        with open(info_file) as f:
            for line in f:
                line = line.strip()
                if line.startswith("was_sparse="):
                    was_sparse = line.split("=", 1)[1] == "1"
                elif line.startswith("raw_super_size="):
                    super_size = int(line.split("=", 1)[1])

    lpmake     = require_tool("lpmake")
    part_args  = []
    total_size = 0

    for img in sorted(glob.glob(os.path.join(parts_dir, "*.img"))):
        pname = os.path.basename(img).replace(".img", "")
        pfmt  = detect_format(img)
        if pfmt == "sparse":
            tmp = tempfile.mktemp(suffix=".raw.img")
            to_raw(img, tmp)
            psize = os.path.getsize(tmp)
            os.remove(tmp)
        else:
            psize = os.path.getsize(img)
        total_size += psize
        part_args += [
            "--partition", f"{pname}:readonly:{psize}:main",
            "--image",     f"{pname}={img}",
        ]
        log(f"  Partition: {pname:<20} {psize//1024//1024} MB")

    metadata_overhead = 65536 * 3 * 2
    device_size = total_size + metadata_overhead + 8 * 1024 * 1024
    device_size = ((device_size + 511) // 512) * 512
    if super_size and super_size > device_size:
        device_size = super_size

    log(f"Super device size: {device_size // 1024 // 1024} MB")

    cmd_args = [
        lpmake,
        "--metadata-size",  "65536",
        "--metadata-slots", "3",
        "--device",         f"super:{device_size}",
        "--group",          f"main:{device_size}",
    ] + part_args + ["--sparse", "--output", output_img]

    run(cmd_args)
    own(os.path.dirname(output_img))

    ok("═" * 50)
    ok("Super repack complete")
    print(f"\n  {W}Output:{N} {output_img}")
    print(f"  {W}Size  :{N} {os.path.getsize(output_img)//1024//1024} MB\n")
