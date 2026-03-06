#!/usr/bin/env python3
"""
lpunpack.py — Pure-Python LP (super.img) partition extractor
Fallback when system lpunpack is not installed.
Supports Android Q+ LP metadata format (magic 0x4D0CC467).
"""

import struct, sys, os, mmap

LP_METADATA_MAGIC          = 0x414C5030   # "ALP0" (alternate check)
LP_METADATA_GEOMETRY_MAGIC = 0x616C4467
LP_SECTOR_SIZE             = 512
LP_METADATA_HEADER_MAGIC   = 0x4D0CC467

LP_PARTITION_ATTR_READONLY = 0x1
LP_PARTITION_ATTR_SLOT_SUFFIXED = 0x2

HEADER_FMT = "<IHHIIIIIIIQi"  # 48 bytes
HEADER_SIZE = struct.calcsize(HEADER_FMT)

GEOMETRY_FMT = "<IIQQII"
GEOMETRY_SIZE = struct.calcsize(GEOMETRY_FMT)

PARTITION_FMT = "<36sIIQ"
PARTITION_SIZE = struct.calcsize(PARTITION_FMT)

EXTENT_FMT = "<QQI"
EXTENT_SIZE = struct.calcsize(EXTENT_FMT)

PARTITION_GROUP_FMT = "<36sIQ"
PARTITION_GROUP_SIZE = struct.calcsize(PARTITION_GROUP_FMT)


def read_geometry(data, offset):
    """Read LP geometry from 4096 + offset bytes in."""
    fields = struct.unpack_from(GEOMETRY_FMT, data, offset)
    magic, struct_size, checksum, metadata_max_size, metadata_slot_count, logical_block_size = fields
    return {
        'magic': magic,
        'metadata_max_size': metadata_max_size,
        'metadata_slot_count': metadata_slot_count,
        'logical_block_size': logical_block_size,
    }


def find_metadata(data):
    """Scan for LP metadata magic at known offsets."""
    candidates = [
        4096,
        8192,
        LP_SECTOR_SIZE,
        LP_SECTOR_SIZE * 2,
    ]
    for off in candidates:
        if off + 4 > len(data):
            continue
        magic = struct.unpack_from("<I", data, off)[0]
        if magic == LP_METADATA_HEADER_MAGIC:
            return off
    # Brute-force scan every sector
    for off in range(0, min(len(data), 0x100000), LP_SECTOR_SIZE):
        if off + 4 > len(data):
            break
        magic = struct.unpack_from("<I", data, off)[0]
        if magic == LP_METADATA_HEADER_MAGIC:
            return off
    return -1


def parse_header(data, offset):
    fields = struct.unpack_from(HEADER_FMT, data, offset)
    (magic, major_version, minor_version, header_size, header_checksum,
     metadata_size, metadata_checksum, tables_size, tables_checksum,
     partitions_offset, partitions_size,  # repurposed fields
     ) = fields[:11]

    # Full header layout (simplified)
    h = struct.unpack_from("<IHH I 32s I 32s I 32s I I I I I I I Q I",
                           data, offset)

    result = {
        'magic': h[0],
        'major': h[1],
        'minor': h[2],
        'header_size': h[3],
        # checksum @ [4]
        'metadata_size': h[5],
        # checksum @ [6]
        'tables_size': h[7],
        # tables_checksum @ [8]
        'partitions_offset': h[9],
        'partitions_count': h[10],
        'extents_offset': h[11],
        'extents_count': h[12],
        'groups_offset': h[13],
        'groups_count': h[14],
        'block_devices_offset': h[15],
        'block_devices_count': h[16],
        'flags': h[17] if len(h) > 17 else 0,
    }
    return result


def parse_partitions(data, tables_offset, partitions_offset, count):
    """Parse partition table entries."""
    partitions = []
    base = tables_offset + partitions_offset
    for i in range(count):
        off = base + i * PARTITION_SIZE
        if off + PARTITION_SIZE > len(data):
            break
        raw = struct.unpack_from(PARTITION_FMT, data, off)
        name_raw, attributes, first_extent_index, num_extents = raw
        name = name_raw.rstrip(b'\x00').decode('utf-8', errors='replace')
        partitions.append({
            'name': name,
            'attributes': attributes,
            'first_extent_index': first_extent_index,
            'num_extents': num_extents,
        })
    return partitions


def parse_extents(data, tables_offset, extents_offset, count):
    """Parse extent table entries."""
    extents = []
    base = tables_offset + extents_offset
    for i in range(count):
        off = base + i * EXTENT_SIZE
        if off + EXTENT_SIZE > len(data):
            break
        raw = struct.unpack_from(EXTENT_FMT, data, off)
        num_sectors, target_type, target_data = raw
        extents.append({
            'num_sectors': num_sectors,
            'target_type': target_type,
            'target_data': target_data,
        })
    return extents


def extract_partition(super_path, partition, extents, out_dir, block_size=512):
    """Extract a partition's data from the super image."""
    name = partition['name']
    if not name:
        return

    out_path = os.path.join(out_dir, name + '.img')
    print(f"  Extracting: {name} → {out_path}")

    total_size = sum(e['num_sectors'] * LP_SECTOR_SIZE for e in extents)

    with open(super_path, 'rb') as sf, open(out_path, 'wb') as of:
        for ext in extents:
            offset = ext['target_data'] * LP_SECTOR_SIZE
            length = ext['num_sectors'] * LP_SECTOR_SIZE
            sf.seek(offset)
            remaining = length
            while remaining > 0:
                chunk = min(remaining, 1024 * 1024)
                buf = sf.read(chunk)
                if not buf:
                    of.write(b'\x00' * chunk)
                else:
                    of.write(buf)
                remaining -= chunk

    print(f"    Size: {total_size // 1024 // 1024} MB")


def lpunpack(super_path, out_dir):
    os.makedirs(out_dir, exist_ok=True)

    with open(super_path, 'rb') as f:
        # Map first 64MB for metadata scanning
        data = f.read(min(os.path.getsize(super_path), 64 * 1024 * 1024))

    print(f"Scanning {super_path} for LP metadata …")
    meta_off = find_metadata(data)
    if meta_off < 0:
        print("ERROR: LP metadata not found in super image", file=sys.stderr)
        sys.exit(1)
    print(f"LP metadata found at offset 0x{meta_off:x}")

    try:
        header = parse_header(data, meta_off)
    except struct.error as e:
        print(f"ERROR: Failed to parse LP header: {e}", file=sys.stderr)
        sys.exit(1)

    if header['magic'] != LP_METADATA_HEADER_MAGIC:
        print(f"ERROR: Bad LP magic: 0x{header['magic']:x}", file=sys.stderr)
        sys.exit(1)

    tables_off = meta_off + header['header_size']

    partitions = parse_partitions(
        data, tables_off,
        header['partitions_offset'],
        header['partitions_count']
    )

    all_extents = parse_extents(
        data, tables_off,
        header['extents_offset'],
        header['extents_count']
    )

    print(f"Found {len(partitions)} partitions, {len(all_extents)} extents")

    for part in partitions:
        name = part['name']
        if not name:
            continue
        # Get extents for this partition
        first = part['first_extent_index']
        count = part['num_extents']
        part_extents = all_extents[first:first + count]

        extract_partition(super_path, part, part_extents, out_dir)

    print(f"Done. Output: {out_dir}")


if __name__ == '__main__':
    if len(sys.argv) < 3:
        print(f"Usage: {sys.argv[0]} <super.img> <out_dir>")
        sys.exit(1)
    lpunpack(sys.argv[1], sys.argv[2])
